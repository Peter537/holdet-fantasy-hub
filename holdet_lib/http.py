"""Small standard-library HTTP client with bounded retries."""

from __future__ import annotations

import errno
from http.client import HTTPException, HTTPResponse, IncompleteRead
import json
from math import isfinite
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

from .errors import FetchError, PayloadError
from .version import VERSION


TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = f"holdet-lib/{VERSION} (Python standard library; public Holdet.dk data)"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
DEFAULT_RETRIES = 2
DEFAULT_CONNECTION_REFUSED_RETRIES = 4
_READ_CHUNK_BYTES = 64 * 1024


def _retry_delay(error: HTTPError | None, attempt: int) -> float:
    if error is not None:
        retry_after = error.headers.get("Retry-After")
        if retry_after and retry_after.isdecimal():
            return min(float(retry_after), 30.0)
    return min(0.5 * (2**attempt), 4.0)


def _is_connection_refused(error: BaseException) -> bool:
    """Return whether *error* represents an actively refused connection."""

    current: object = error
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "winerror", None) == 10061:
            return True
        if getattr(current, "errno", None) in {errno.ECONNREFUSED, 10061}:
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            current = reason
            continue
        break
    return False


def _configured_proxy_for_url(url: str) -> str | None:
    """Return the proxy urllib will use for *url*, respecting bypass rules."""

    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname or proxy_bypass(parsed.hostname):
        return None
    return getproxies().get(parsed.scheme.casefold())


def _redact_proxy_url(proxy_url: str) -> str:
    """Return a diagnostic-safe proxy address without credentials."""

    candidate = proxy_url if "://" in proxy_url else f"//{proxy_url}"
    parsed = urlsplit(candidate)
    if parsed.hostname is None:
        return "<configured proxy>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    credentials = "***@" if parsed.username is not None else ""
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}{credentials}{host}{port}"


class HttpClient:
    """Fetch public HTML and JSON using one retry policy."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        retries: int = DEFAULT_RETRIES,
        connection_refused_retries: int = DEFAULT_CONNECTION_REFUSED_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        proxy_resolver: Callable[[str], str | None] = _configured_proxy_for_url,
    ) -> None:
        if retries < 0 or connection_refused_retries < 0:
            raise ValueError("Antallet af genforsøg må ikke være negativt")
        if not all(value > 0 and isfinite(value) for value in (timeout, total_timeout)):
            raise ValueError("Timeout skal være et positivt, endeligt tal")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("Svaret skal have en positiv størrelsesgrænse i bytes")
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.max_response_bytes = max_response_bytes
        self.retries = retries
        self.connection_refused_retries = connection_refused_retries
        self.sleep = sleep
        self.clock = clock
        self.proxy_resolver = proxy_resolver

    def _remaining(self, url: str, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise FetchError(f"Tidsgrænsen for hentning af {url} er overskredet")
        return remaining

    def _read_body(self, response: HTTPResponse, url: str, deadline: float) -> bytearray:
        expected_length = getattr(response, "length", None)
        size_error = f"Svaret fra {url} overskrider størrelsesgrænsen på {self.max_response_bytes} bytes"
        if expected_length is not None and expected_length > self.max_response_bytes:
            raise FetchError(size_error)

        # read1 returns available data without waiting to fill an entire buffer.
        # An ordinary read(size) can hide steady progress until socket timeout.
        read = getattr(response, "read1", None) or response.read
        body = bytearray()
        while True:
            self._remaining(url, deadline)
            capacity = self.max_response_bytes - len(body)
            chunk = read(min(_READ_CHUNK_BYTES, capacity + 1))
            self._remaining(url, deadline)
            if not chunk:
                break
            if len(chunk) > capacity:
                raise FetchError(size_error)
            body.extend(chunk)
        if expected_length is not None and len(body) < expected_length:
            raise IncompleteRead(bytes(body), expected_length - len(body))
        return body

    def fetch_text(self, url: str, *, accept: str = "text/html") -> str:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Encoding": "identity",
            },
        )
        deadline = self.clock() + self.total_timeout
        attempt = 0
        while True:
            timeout = min(self.timeout, self._remaining(url, deadline))
            try:
                with urlopen(request, timeout=timeout) as response:
                    body = self._read_body(response, url, deadline)
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        return body.decode(charset)
                    except (LookupError, UnicodeDecodeError) as exc:
                        raise FetchError(
                            f"URL'en {url} kunne ikke afkodes med {charset!r}"
                        ) from exc
            except HTTPError as exc:
                try:
                    if exc.code not in TRANSIENT_HTTP_STATUSES or attempt >= self.retries:
                        raise FetchError(f"HTTP {exc.code} under hentning af {url}") from exc
                    delay = _retry_delay(exc, attempt)
                finally:
                    exc.close()
            except (OSError, HTTPException) as exc:
                retry_limit = (
                    max(self.retries, self.connection_refused_retries)
                    if _is_connection_refused(exc)
                    else self.retries
                )
                if attempt >= retry_limit:
                    reason = getattr(exc, "reason", exc)
                    try:
                        proxy = self.proxy_resolver(url)
                        route = f" via configured proxy {_redact_proxy_url(proxy)}" if proxy else ""
                    except Exception:
                        route = ""
                    raise FetchError(
                        f"Kunne ikke hente {url}{route}: {reason}"
                    ) from exc
                delay = _retry_delay(None, attempt)
            if delay >= self._remaining(url, deadline):
                raise FetchError(f"Tidsgrænsen for hentning af {url} tillader ikke et nyt forsøg")
            self.sleep(delay)
            attempt += 1

    def fetch_json(self, url: str) -> object:
        text = self.fetch_text(url, accept="application/json")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if text.lstrip("\ufeff \t\r\n").lower().startswith(("<!doctype html", "<html")):
                raise PayloadError(
                    f"URL'en {url} returnerede en HTML-side i stedet for JSON"
                ) from exc
            raise PayloadError(f"URL'en {url} returnerede ugyldig JSON") from exc


def fetch_html(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    retries: int = DEFAULT_RETRIES,
    connection_refused_retries: int = DEFAULT_CONNECTION_REFUSED_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Compatibility wrapper used by the original player workflow."""

    return HttpClient(
        timeout=timeout,
        total_timeout=total_timeout,
        max_response_bytes=max_response_bytes,
        retries=retries,
        connection_refused_retries=connection_refused_retries,
        sleep=sleep,
    ).fetch_text(url)
