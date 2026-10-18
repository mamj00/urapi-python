from __future__ import annotations
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import traceback
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from urapi import UrapiClient, UrapiClientError, UrapiApiError, OPERATION_IDS

KEY = "ur_test_" + "a" * 24 + "." + "b" * 43
INTENT = "00000000-0000-4000-8000-000000000001"


@contextmanager
def server(status=200, payload=b'{"id":"job_fixture"}', headers=None, delay=0, disconnect=False):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, dict(self.headers), body))
            if disconnect:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            time.sleep(delay)
            self.send_response(status)
            for name, value in (headers or {"Content-Type": "application/json"}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        do_POST = do_GET
        do_DELETE = do_GET
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def client(origin, **kwargs):
    return UrapiClient(base_url=origin, api_key=KEY, allow_http_loopback_for_tests=True, **kwargs)


class ClientTests(unittest.TestCase):
    def test_catalog_and_actual_query_bytes(self):
        self.assertEqual(len(OPERATION_IDS), 45)
        with server() as (origin, seen):
            result = client(origin).request("listJobs", {"query": {"cursor": "a+/=", "limit": 10, "state": "failed"}})
            self.assertEqual(result.data["id"], "job_fixture")
            self.assertEqual(seen[0][1], "/v1/jobs?cursor=a%2B%2F%3D&limit=10&state=failed")
            self.assertEqual(seen[0][2]["Authorization"], "Bearer " + KEY)
            self.assertNotIn(KEY, repr(client(origin)))

    def test_write_preserves_explicit_intent_body_and_does_not_retry(self):
        with server(202) as (origin, seen):
            body = {"text": "Hola 😀", "max_credits": "1.000"}
            client(origin).request("createQR", {"body": body, "idempotency_key": INTENT})
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][2]["Idempotency-Key"], INTENT)
            self.assertEqual(json.loads(seen[0][3]), body)

    def test_input_rejected_before_transport(self):
        invalid = [("getJob", {}), ("getJob", {"path": {"job_id": ".."}}),
                   ("listJobs", {"query": {"limit": True}}), ("listJobs", {"query": {"limit": 101}}),
                   ("listJobs", {"query": {"url": "https://other.invalid"}}),
                   ("getJob", {"path": {"job_id": "other/tenant"}}),
                   ("createQR", {"body": {}}), ("createQR", {"idempotency_key": INTENT}),
                   ("getJob", {"path": {"job_id": "safe"}, "body": {}}),
                   ("getJob", {"path": {"job_id": "safe"}, "idempotency_key": INTENT}),
                   ("listJobs", {"query": {"cursor": "\ud800"}}),
                   ("listJobs", {"headers": {"Authorization": "other"}})]
        with server() as (origin, seen):
            sdk = client(origin)
            for operation, values in invalid:
                with self.subTest(operation=operation, keys=list(values)):
                    with self.assertRaises(UrapiClientError) as caught:
                        sdk.request(operation, values)
                    self.assertFalse(caught.exception.outcome_unknown)
            self.assertEqual(seen, [])

    def test_json_structure_bounds(self):
        cycle = []; cycle.append(cycle)
        for value in [float("nan"), float("inf"), 10**1000, {1: "bad"}, (1, 2), cycle, "\ud800"]:
            with self.subTest(kind=type(value).__name__), self.assertRaises(UrapiClientError):
                client("http://127.0.0.1:1").request("createQR", {"body": value, "idempotency_key": INTENT})
        with self.assertRaises(UrapiClientError) as caught:
            client("http://127.0.0.1:1", max_request_bytes=256).request("createQR", {"body": {"text": "a"*300}, "idempotency_key": INTENT})
        self.assertEqual(caught.exception.code, "request_too_large")

    def test_configuration_closed(self):
        urls = ["http://localhost:4100", "http://127.1", "http://127.0.0.01", "https://user:pass@example.com", "https://example.com/v1", "https://example.com?x", "https://example.com#x", "https://EXAMPLE.com", "https://example.com\\@other.invalid", "https://%65xample.com", " https://example.com", "https://example.com\n", "https://example.com:0", "ftp://example.com"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UrapiClientError):
                client(url)
        for kwargs in [{"api_key": KEY.replace("ur_test_", "ur_live_")}, {"allow_http_loopback_for_tests": False}]:
            config = dict(base_url="http://127.0.0.1:4100", api_key=KEY, allow_http_loopback_for_tests=True)
            config.update(kwargs)
            with self.assertRaises(UrapiClientError):
                UrapiClient(**config)

    def test_https_verification_enabled(self):
        with patch("urapi.client.http.client.HTTPSConnection") as connection:
            connection.side_effect = OSError("fixture")
            with self.assertRaises(UrapiClientError):
                UrapiClient(base_url="https://api.example.invalid", api_key=KEY).request("listJobs")
            context = connection.call_args.kwargs["context"]
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def test_proxy_environment_ignored(self):
        with server() as (origin, seen), patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            client(origin).request("listJobs")
            self.assertEqual(len(seen), 1)

    def test_redirect_not_followed(self):
        with server() as (destination, target_seen), server(302, b"", {"Location": destination + "/steal"}) as (origin, seen):
            with self.assertRaises(UrapiClientError):
                client(origin).request("listJobs")
            self.assertEqual(len(seen), 1)
            self.assertEqual(target_seen, [])

    def test_status_error_redaction_and_unknown_write(self):
        payload = json.dumps({"code": KEY, "detail": KEY}).encode()
        with server(503, payload, {"Content-Type": "application/problem+json", "X-Request-Id": KEY, "Retry-After": "99999999"}) as (origin, seen):
            with self.assertRaises(UrapiApiError) as caught:
                client(origin).request("createQR", {"body": {}, "idempotency_key": INTENT})
            error = caught.exception
            self.assertEqual(error.status, 503)
            self.assertEqual(error.code, "http_error")
            self.assertIsNone(error.request_id)
            self.assertEqual(error.retry_after_seconds, 86400)
            self.assertTrue(error.outcome_unknown)
            self.assertNotIn(KEY, repr(error))
            self.assertEqual(len(seen), 1)

    def test_safe_error_metadata(self):
        with server(403, b'{"code":"scope_denied"}', {"Content-Type": "application/problem+json", "X-Request-Id": "req_fixture"}) as (origin, _):
            with self.assertRaises(UrapiApiError) as caught:
                client(origin).request("listJobs")
            self.assertEqual(caught.exception.code, "scope_denied")
            self.assertEqual(caught.exception.request_id, "req_fixture")
            self.assertFalse(caught.exception.outcome_unknown)

    def test_response_bounds_and_invalid_json(self):
        cases = [(b"x"*300, {"Content-Type": "application/json"}, "response_too_large"),
                 (b"", {"Content-Length": "999999", "Content-Type": "application/json"}, "response_too_large"),
                 (b'{"x":NaN}', {"Content-Type": "application/json"}, "invalid_response"),
                 (b'{"x":1e999}', {"Content-Type": "application/json"}, "invalid_response"),
                 (b'{"ok":true}', {"Content-Type": "application/json", "Content-Length": "100"}, "invalid_response"),
                 (b"[]", {"Content-Type": "application/json"}, "invalid_response"),
                 (b"{}", {"Content-Type": "text/html"}, "invalid_response"),
                 (b"{}", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, "invalid_response")]
        for payload, headers, expected in cases:
            with self.subTest(expected=expected), server(200, payload, headers) as (origin, _):
                with self.assertRaises(UrapiClientError) as caught:
                    client(origin, max_response_bytes=256).request("listJobs")
                self.assertEqual(caught.exception.code, expected)

    def test_huge_timeout_and_truncated_write_are_typed(self):
        with self.assertRaises(UrapiClientError) as caught:
            client("http://127.0.0.1:1").request("listJobs", options={"timeout_seconds": 10**1000})
        self.assertEqual(caught.exception.code, "invalid_input")
        with server(202, b'{"id":"job_fixture"}', {"Content-Type": "application/json", "Content-Length": "100"}) as (origin, seen):
            with self.assertRaises(UrapiClientError) as caught:
                client(origin).request("createQR", {"body": {}, "idempotency_key": INTENT})
            self.assertEqual(caught.exception.code, "invalid_response")
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(len(seen), 1)

    def test_timeout_and_lost_response_no_retry(self):
        for settings, expected in [({"delay": 0.15}, "request_timeout"), ({"disconnect": True}, "transport_error")]:
            with server(**settings) as (origin, seen):
                with self.assertRaises(UrapiClientError) as caught:
                    client(origin, timeout_seconds=0.05).request("createQR", {"body": {}, "idempotency_key": INTENT})
                self.assertEqual(caught.exception.code, expected)
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertEqual(len(seen), 1)

    def test_transport_exception_not_chained_or_leaked(self):
        with patch("urapi.client.http.client.HTTPConnection", side_effect=OSError(KEY)):
            try:
                client("http://127.0.0.1:1").request("listJobs")
            except UrapiClientError as error:
                self.assertIsNone(error.__context__)
                self.assertIsNone(error.__cause__)
                self.assertNotIn(KEY, "".join(traceback.format_exception(error)))
            else:
                self.fail("Expected sanitized failure")


if __name__ == "__main__":
    unittest.main()
