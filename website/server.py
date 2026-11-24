"""Canonical Streamlit ASGI wrapper with a loopback-only read-only API."""

from __future__ import annotations

from contextlib import asynccontextmanager
import csv
from datetime import timezone
from email.utils import format_datetime
import hashlib
from io import StringIO
import ipaddress
import json
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

import streamlit as st
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

from holdet_lib.local_api import (
    ApiQueryError,
    LocalDataApi,
    dataset_catalog,
    dataset_definition,
    resolve_registered_artifact,
)
from holdet_lib.data_packages import neutralize_spreadsheet_text
from holdet_lib.errors import PayloadError
from holdet_lib.paths import resolve_paths
from holdet_lib.restore_fence import restore_read_version
from holdet_lib.refresh_runs import RefreshRunBusyError
from holdet_lib.restore_transaction import recover_restore


APP_PATHS = resolve_paths()
DATA_API = LocalDataApi(APP_PATHS)
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_DATA_PARAMS = frozenset(
    {"format", "limit", "offset", "locale", "game", "round", "group", "season", "team_id"}
)
# Commas inside an opaque tag belong to its value.
_ENTITY_TAG_ITEM = re.compile(r'[ \t,]*(?:W/)?("[\x21\x23-\x7e\x80-\xff]*")[ \t]*(?:,|$)')


def _json(payload: object, status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )


class LoopbackSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = urlsplit("//" + request.headers.get("host", "")).hostname
        client_host = request.client.host if request.client is not None else ""
        try:
            client_is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            client_is_loopback = False
        if host not in _ALLOWED_HOSTS or not client_is_loopback:
            response = _json(
                {"error": {"code": "loopback_required", "message": "Kun lokale forespørgsler er tilladt."}},
                403,
            )
        elif request.url.path.startswith(("/api/v1/data/", "/downloads/")):
            try:
                version = await run_in_threadpool(restore_read_version, APP_PATHS)
            except PayloadError:
                response = _restore_unavailable()
            else:
                response = await call_next(request)
                try:
                    current = await run_in_threadpool(restore_read_version, APP_PATHS)
                except PayloadError:
                    response = _restore_unavailable()
                else:
                    if current != version:
                        response = _restore_unavailable()
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


def _restore_unavailable() -> Response:
    return _json(
        {"error": {"code": "restore_pending", "message": "Data gendannes. Prøv forespørgslen igen."}},
        503,
    )


async def health(request: Request) -> Response:
    return _json(
        {
            "status": "ok",
            "api_version": "v1",
            "mode": "read-only",
            "network_access": False,
        }
    )


async def catalog(request: Request) -> Response:
    return _json(dataset_catalog())


def _openapi_document() -> dict[str, object]:
    data_paths = {
        f"/api/v1/data/{item['name']}": {
            "get": {
                "summary": f"Læs {item['name']}",
                "parameters": [
                    {"name": name, "in": "query", "required": name in item["required_filters"], "schema": {"type": "string"}}
                    for name in [*item["filters"], "format", "limit", "offset"]
                ],
                "responses": {"200": {"description": "Lokale data"}, "304": {"description": "Ikke ændret"}},
            },
            "head": {"summary": f"Metadata for {item['name']}", "responses": {"200": {"description": "Metadata"}}},
        }
        for item in dataset_catalog()["datasets"]  # type: ignore[index]
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Holdet Fantasy Hub local API", "version": "1.0.0"},
        "servers": [{"url": "/"}],
        "paths": {
            "/api/v1/health": {"get": {"responses": {"200": {"description": "Status"}}}},
            "/api/v1/catalog": {"get": {"responses": {"200": {"description": "Datasætkatalog"}}}},
            **data_paths,
        },
    }


async def openapi(request: Request) -> Response:
    return _json(_openapi_document())


def _parse_integer(value: str | None, name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ApiQueryError(f"{name} skal være et heltal") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        upper = f"–{maximum}" if maximum is not None else f" eller større"
        raise ApiQueryError(f"{name} skal være {minimum}{upper}")
    return parsed


def _csv_bytes(dataset: str, rows: tuple[dict[str, object], ...]) -> bytes:
    definition = dataset_definition(dataset)
    output = StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n")
    writer.writerow(definition.columns)
    for row in rows:
        writer.writerow(
            ""
            if row.get(column) is None
            else (
                neutralize_spreadsheet_text(str(row[column]))
                if isinstance(row[column], str)
                else row[column]
            )
            for column in definition.columns
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _conditional_headers(body: bytes, last_modified) -> dict[str, str]:
    headers = {"ETag": '"' + hashlib.sha256(body).hexdigest() + '"', "Cache-Control": "private, no-cache"}
    if last_modified is not None:
        headers["Last-Modified"] = format_datetime(last_modified.astimezone(timezone.utc), usegmt=True)
    return headers


def _if_none_match(header_values: list[str], etag: str) -> bool:
    """Apply the weak comparison and list syntax for GET and HEAD."""
    value = ",".join(header_values)
    if value.strip(" \t") == "*":
        return True
    matched = False
    offset = 0
    while offset < len(value):
        item = _ENTITY_TAG_ITEM.match(value, offset)
        if item is None:
            return matched and not value[offset:].strip(" \t,")
        matched |= item.group(1) == etag
        offset = item.end()
    return matched


def data_endpoint(request: Request) -> Response:
    dataset = request.path_params["dataset"]
    try:
        pairs = request.query_params.multi_items()
        names = [name for name, _ in pairs]
        if len(names) != len(set(names)):
            raise ApiQueryError("Et query-parameter må kun angives én gang")
        unknown = set(names) - _ALLOWED_DATA_PARAMS
        if unknown:
            raise ApiQueryError("Ukendte query-parametre: " + ", ".join(sorted(unknown)))
        output_format = request.query_params.get("format", "json").casefold()
        if output_format not in {"json", "csv"}:
            raise ApiQueryError("format skal være json eller csv")
        limit = _parse_integer(request.query_params.get("limit"), "limit", 1000, 1, 5000)
        offset = _parse_integer(request.query_params.get("offset"), "offset", 0, 0)
        filters = {
            name: value
            for name, value in pairs
            if name not in {"format", "limit", "offset"}
        }
        all_rows = DATA_API.rows(dataset, filters)
        selected = all_rows[offset : offset + limit]
        if output_format == "csv":
            body = _csv_bytes(dataset, selected)
            media_type = "text/csv; charset=utf-8"
        else:
            body = json.dumps(
                {
                    "dataset": dataset,
                    "count": len(selected),
                    "total": len(all_rows),
                    "limit": limit,
                    "offset": offset,
                    "rows": selected,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            media_type = "application/json"
        headers = _conditional_headers(body, DATA_API.last_modified())
        if _if_none_match(request.headers.getlist("if-none-match"), headers["ETag"]):
            return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(body))
        if request.method == "HEAD":
            return Response(b"", media_type=media_type, headers=headers)
        return Response(body, media_type=media_type, headers=headers)
    except ApiQueryError as exc:
        return _json({"error": {"code": "invalid_query", "message": str(exc)}}, 400)
    except Exception:
        return _json(
            {"error": {"code": "local_data_error", "message": "De lokale data kunne ikke læses."}},
            500,
        )


def download_artifact(request: Request) -> Response:
    path = resolve_registered_artifact(APP_PATHS, request.path_params["artifact_id"])
    if path is None:
        return _json({"error": {"code": "not_found", "message": "Artifactet findes ikke eller er ændret."}}, 404)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@asynccontextmanager
async def _restore_on_startup(_app):
    try:
        await run_in_threadpool(recover_restore, APP_PATHS)
    except (OSError, PayloadError, RefreshRunBusyError) as exc:
        logging.getLogger(__name__).warning("Restore recovery is pending (%s)", type(exc).__name__)
    yield


app = st.App(
    Path(__file__).with_name("app.py"),
    lifespan=_restore_on_startup,
    routes=[
        Route("/api/v1/health", health, methods=["GET"]),
        Route("/api/v1/catalog", catalog, methods=["GET"]),
        Route("/api/v1/openapi.json", openapi, methods=["GET"]),
        Route("/api/v1/data/{dataset}", data_endpoint, methods=["GET", "HEAD"]),
        Route("/downloads/{artifact_id}", download_artifact, methods=["GET", "HEAD"]),
    ],
    middleware=[Middleware(LoopbackSecurityMiddleware)],
)
