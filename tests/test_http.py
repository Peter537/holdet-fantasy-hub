from __future__ import annotations

import errno
from http.client import IncompleteRead
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

    def __init__(self, body: bytes = b"ok", *, length: int | None = None) -> None:
        self.body = body
        self.length = length
        self.read_sizes: list[int] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        result, self.body = self.body[:size], self.body[size:]
        return result


class HttpRetryTests(unittest.TestCase):
    def test_retries_short_body_when_read1_reaches_eof(self) -> None:
        short = _Response(b"hi", length=3)
        complete = _Response(b"yes", length=3)
        sleeps = []
        with patch("holdet_lib.http.urlopen", side_effect=[short, complete]) as opener:
            result = holdet.HttpClient(sleep=sleeps.append).fetch_text("https://example.test")
        self.assertEqual(result, "yes")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(sleeps, [0.5])
        self.assertTrue(short.closed and complete.closed)

    def test_incomplete_read_is_normalized_when_retry_budget_is_exhausted(self) -> None:
        response = _Response()
        with (
            patch.object(response, "read1", side_effect=IncompleteRead(b"x", 1)),
            patch("holdet_lib.http.urlopen", return_value=response) as opener,
        ):
            with self.assertRaises(holdet.FetchError) as raised:
                holdet.HttpClient(retries=0).fetch_text("https://example.test")
        self.assertIsInstance(raised.exception.__cause__, IncompleteRead)
        self.assertEqual(opener.call_count, 1)
        self.assertTrue(response.closed)

    def test_oversized_body_is_never_retried(self) -> None:
        for declared, read_sizes in ((None, [5]), (5, [])):
            with self.subTest(declared=declared):
                response = _Response(b"extra", length=declared)
                with patch("holdet_lib.http.urlopen", return_value=response) as opener:
                    with self.assertRaisesRegex(holdet.FetchError, "størrelsesgrænsen"):
                        holdet.HttpClient(max_response_bytes=4).fetch_text("https://example.test")
                self.assertEqual(opener.call_count, 1)
                self.assertEqual(response.read_sizes, read_sizes)
                self.assertTrue(response.closed)

    def test_deadline_covers_progressing_reads_and_limits_socket_timeout(self) -> None:
        now = 0.0

        class SlowResponse(_Response):
            def read1(self, size):
                nonlocal now
                now += 0.6
                return super().read1(min(size, 1))

        response = SlowResponse(b"123")
        with patch("holdet_lib.http.urlopen", return_value=response) as opener:
            with self.assertRaisesRegex(holdet.FetchError, "Tidsgrænsen"):
                holdet.HttpClient(clock=lambda: now, total_timeout=1).fetch_text("https://example.test")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.kwargs["timeout"], 1)
        self.assertEqual(len(response.read_sizes), 2)
        self.assertTrue(response.closed)

    def test_retry_uses_remaining_deadline_after_wait(self) -> None:
        now = 0.0

        def sleep(delay):
            nonlocal now
            now += delay

        with patch("holdet_lib.http.urlopen", side_effect=[TimeoutError(), _Response()]) as opener:
            client = holdet.HttpClient(clock=lambda: now, sleep=sleep, timeout=1, total_timeout=1.25)
            self.assertEqual(client.fetch_text("https://example.test"), "ok")
        self.assertEqual([call.kwargs["timeout"] for call in opener.call_args_list], [1, 0.75])

    def test_backoff_is_skipped_when_no_retry_fits(self) -> None:
        sleeps = []
        with patch("holdet_lib.http.urlopen", side_effect=TimeoutError()) as opener:
            with self.assertRaisesRegex(holdet.FetchError, "Tidsgrænsen"):
                holdet.HttpClient(clock=lambda: 0, total_timeout=0.25, sleep=sleeps.append).fetch_text(
                    "https://example.test"
                )
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_html_response_instead_of_json_has_actionable_error(self) -> None:
        with patch("holdet_lib.http.urlopen", return_value=_Response(b"<!DOCTYPE html><html><title>Holdet.dk</title></html>")):
            with self.assertRaisesRegex(holdet.PayloadError, "HTML-side i stedet for JSON"):
                holdet.HttpClient().fetch_json("https://example.test/api/cartridge")

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
