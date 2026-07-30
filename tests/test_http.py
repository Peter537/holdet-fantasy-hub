from __future__ import annotations

import errno
from urllib.error import HTTPError, URLError
import unittest
from unittest.mock import patch

import holdet_lib as holdet
import holdet_lib.http as http_module


class _Headers(dict):
    def get_content_charset(self):
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, body: bytes = b"ok") -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class HttpRetryTests(unittest.TestCase):
    def test_direct_connection_refusal_can_succeed_on_fifth_attempt(self) -> None:
        sleeps: list[float] = []
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        side_effects = [refused, refused, refused, refused, _Response()]
        with patch("holdet_lib.http.urlopen", side_effect=side_effects) as mocked:
            result = holdet.HttpClient(sleep=sleeps.append).fetch_text("https://example.test")

        self.assertEqual(result, "ok")
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0, 4.0])

    def test_url_error_wrapped_connection_refusal_uses_extended_budget(self) -> None:
        sleeps: list[float] = []
        refused = URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
        side_effects = [refused, refused, refused, refused, _Response()]
        with patch("holdet_lib.http.urlopen", side_effect=side_effects) as mocked:
            result = holdet.HttpClient(sleep=sleeps.append).fetch_text("https://example.test")

        self.assertEqual(result, "ok")
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0, 4.0])

    def test_timeout_keeps_regular_three_attempt_budget(self) -> None:
        sleeps: list[float] = []
        with patch("holdet_lib.http.urlopen", side_effect=TimeoutError("slow")) as mocked:
            with self.assertRaises(holdet.FetchError):
                holdet.HttpClient(sleep=sleeps.append).fetch_text("https://example.test")

        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_permanent_http_error_is_not_retried(self) -> None:
        error = HTTPError("https://example.test", 404, "missing", {}, None)
        self.addCleanup(error.close)
        with patch("holdet_lib.http.urlopen", side_effect=error) as mocked:
            with self.assertRaisesRegex(holdet.FetchError, "HTTP 404"):
                holdet.HttpClient(sleep=lambda _delay: None).fetch_text(
                    "https://example.test"
                )

        self.assertEqual(mocked.call_count, 1)

    def test_windows_error_10061_uses_extended_budget(self) -> None:
        class WindowsRefusedError(OSError):
            winerror = 10061

        sleeps: list[float] = []
        refused = WindowsRefusedError("actively refused")
        with patch(
            "holdet_lib.http.urlopen",
            side_effect=[refused, refused, _Response()],
        ) as mocked:
            result = holdet.HttpClient(
                retries=0,
                connection_refused_retries=2,
                sleep=sleeps.append,
            ).fetch_text("https://example.test")

        self.assertEqual(result, "ok")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_transient_http_error_keeps_regular_budget(self) -> None:
        sleeps: list[float] = []
        unavailable = HTTPError(
            "https://example.test", 503, "unavailable", {}, None
        )
        self.addCleanup(unavailable.close)
        with patch(
            "holdet_lib.http.urlopen",
            side_effect=[unavailable, unavailable, _Response()],
        ) as mocked:
            result = holdet.HttpClient(sleep=sleeps.append).fetch_text(
                "https://example.test"
            )

        self.assertEqual(result, "ok")
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])


    def test_retry_budgets_are_injectable_and_validated(self) -> None:
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        with patch("holdet_lib.http.urlopen", side_effect=refused) as mocked:
            with self.assertRaises(holdet.FetchError):
                holdet.HttpClient(
                    retries=0,
                    connection_refused_retries=1,
                    sleep=lambda _delay: None,
                ).fetch_text("https://example.test")
        self.assertEqual(mocked.call_count, 2)
        with self.assertRaises(ValueError):
            holdet.HttpClient(connection_refused_retries=-1)


    def test_proxy_diagnostic_redacts_credentials(self) -> None:
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        client = holdet.HttpClient(
            retries=0,
            connection_refused_retries=0,
            sleep=lambda _delay: None,
            proxy_resolver=lambda _url: "http://alice:secret@proxy.test:8080",
        )
        with patch("holdet_lib.http.urlopen", side_effect=refused):
            with self.assertRaises(holdet.FetchError) as raised:
                client.fetch_text("https://example.test")

        message = str(raised.exception)
        self.assertIn(
            "via configured proxy http://***@proxy.test:8080",
            message,
        )
        self.assertNotIn("alice", message)
        self.assertNotIn("secret", message)

    def test_direct_connection_diagnostic_does_not_mention_proxy(self) -> None:
        refused = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        client = holdet.HttpClient(
            retries=0,
            connection_refused_retries=0,
            sleep=lambda _delay: None,
            proxy_resolver=lambda _url: None,
        )
        with patch("holdet_lib.http.urlopen", side_effect=refused):
            with self.assertRaises(holdet.FetchError) as raised:
                client.fetch_text("https://example.test")

        self.assertNotIn("proxy", str(raised.exception))

    def test_proxy_resolution_respects_urllib_bypass_rules(self) -> None:
        proxies = {"https": "http://proxy.test:8080"}
        with (
            patch("holdet_lib.http.getproxies", return_value=proxies),
            patch("holdet_lib.http.proxy_bypass", return_value=False),
        ):
            self.assertEqual(
                http_module._configured_proxy_for_url("https://example.test/data"),
                "http://proxy.test:8080",
            )
        with (
            patch("holdet_lib.http.getproxies", return_value=proxies),
            patch("holdet_lib.http.proxy_bypass", return_value=True),
        ):
            self.assertIsNone(
                http_module._configured_proxy_for_url("https://example.test/data")
            )

if __name__ == "__main__":
    unittest.main()
