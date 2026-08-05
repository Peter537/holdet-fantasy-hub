"""Small standard-library HTTP client with bounded retries."""

from __future__ import annotations

import errno
import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

from .errors import FetchError, PayloadError
from .version import VERSION


TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = f"holdet-lib/{VERSION} (Python standard library; public Holdet.dk data)"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 2
DEFAULT_CONNECTION_REFUSED_RETRIES = 4


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
        retries: int = DEFAULT_RETRIES,
        connection_refused_retries: int = DEFAULT_CONNECTION_REFUSED_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        proxy_resolver: Callable[[str], str | None] = _configured_proxy_for_url,
    ) -> None:
        if retries < 0 or connection_refused_retries < 0:
            raise ValueError("Antallet af genforsøg må ikke være negativt")
        self.timeout = timeout
        self.retries = retries
        self.connection_refused_retries = connection_refused_retries
        self.sleep = sleep
        self.proxy_resolver = proxy_resolver

    def fetch_text(self, url: str, *, accept: str = "text/html") -> str:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "Accept-Encoding": "identity",
            },
        )
        last_error: Exception | None = None
        attempt = 0
        while True:
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        return body.decode(charset)
                    except (LookupError, UnicodeDecodeError) as exc:
                        raise FetchError(
                            f"URL'en {url} kunne ikke afkodes med {charset!r}"
                        ) from exc
            except HTTPError as exc:
                last_error = exc
                if (
                    exc.code not in TRANSIENT_HTTP_STATUSES
                    or attempt >= self.retries
                ):
                    raise FetchError(
                        f"HTTP {exc.code} under hentning af {url}"
                    ) from exc
                self.sleep(_retry_delay(exc, attempt))
            except (TimeoutError, URLError, OSError) as exc:
                last_error = exc
                retry_limit = (
                    max(self.retries, self.connection_refused_retries)
                    if _is_connection_refused(exc)
                    else self.retries
                )
                if attempt >= retry_limit:
                    reason = getattr(exc, "reason", exc)
                    try:
                        proxy = self.proxy_resolver(url)
                    except Exception:
                        proxy = None
                    route = (
                        f" via configured proxy {_redact_proxy_url(proxy)}"
                        if proxy
                        else ""
                    )
                    raise FetchError(
                        f"Kunne ikke hente {url}{route}: {reason}"
                    ) from exc
                self.sleep(_retry_delay(None, attempt))
            attempt += 1
        raise FetchError(f"Kunne ikke hente {url}: {last_error}")

    def fetch_json(self, url: str) -> object:
        text = self.fetch_text(url, accept="application/json")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"URL'en {url} returnerede ugyldig JSON") from exc


def fetch_html(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    connection_refused_retries: int = DEFAULT_CONNECTION_REFUSED_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Compatibility wrapper used by the original player workflow."""

    return HttpClient(
        timeout=timeout,
        retries=retries,
        connection_refused_retries=connection_refused_retries,
        sleep=sleep,
    ).fetch_text(url)
