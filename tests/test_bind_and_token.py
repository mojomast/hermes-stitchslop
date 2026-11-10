"""Catalog-readiness: loopback bind, relay auth, nav allowlist, pane-only pairing."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class BindHosts(unittest.TestCase):
    def setUp(self):
        self.browser = load("stitchslop_browser_bind_test", "browser.py")

    def test_default_is_loopback_only(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STITCHSLOP_PANE_BIND", None)
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1"])

    def test_override_list_keeps_loopback(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "100.64.0.1"}):
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1", "100.64.0.1"])

    def test_refuses_wildcard(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "0.0.0.0"}):
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1"])

    def test_refuses_unspecified_v6(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "::"}):
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1"])

    def test_refuses_public_and_lan(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "8.8.8.8,192.168.1.1,10.0.0.1"}):
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1"])

    def test_allows_explicit_tailscale_v6(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "fd7a:115c:a1e0::1"}):
            self.assertEqual(self.browser.bind_hosts(), ["127.0.0.1", "fd7a:115c:a1e0::1"])

    def test_classify_denied_wildcard_and_link_local(self):
        classify = self.browser.classify_bind_host
        self.assertEqual(classify("0.0.0.0"), "denied")
        self.assertEqual(classify("::"), "denied")
        self.assertEqual(classify("*"), "denied")
        self.assertEqual(classify("169.254.1.1"), "denied")
        self.assertEqual(classify("localhost"), "denied")
        self.assertEqual(classify("evil.example"), "denied")

    def test_public_host_prefers_tailnet(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "100.64.1.8"}):
            self.assertEqual(self.browser.public_host(), "100.64.1.8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STITCHSLOP_PANE_BIND", None)
            self.assertEqual(self.browser.public_host(), "127.0.0.1")


class NavAllowlist(unittest.TestCase):
    def setUp(self):
        self.browser = load("stitchslop_browser_nav_test", "browser.py")

    def test_allows_stitchslop_origins(self):
        for url in (
            "https://www.stitchslop.com/",
            "https://stitchslop.com/",
            "https://www.stitchslop.com/agent",
            "https://app.stitchslop.com/design",
        ):
            ok, reason = self.browser.allowed_nav_url(url)
            self.assertTrue(ok, msg=f"{url}: {reason}")

    def test_rejects_unsafe_urls(self):
        for url in (
            "file:///home/user/.hermes/.env",
            "http://www.stitchslop.com/",
            "https://evil.example/",
            "https://stitchslop.com.evil.example/",
            "https://127.0.0.1/",
            "https://192.168.1.1/admin",
            "https://user:pass@www.stitchslop.com/",
            "https://www.stitchslop.com:8443/",
            "javascript:alert(1)",
            "data:text/html,hi",
            "",
        ):
            ok, reason = self.browser.allowed_nav_url(url)
            self.assertFalse(ok, msg=f"should reject {url}")
            self.assertTrue(reason)

    def test_dispatch_blocks_forbidden_nav_and_allows_site(self):
        class FakeWS:
            def __init__(self):
                self.sent = []

            def send(self, msg):
                self.sent.append(msg)

        relay = self.browser.Relay()
        relay.ws = FakeWS()
        blocked = relay.dispatch({"kind": "nav", "url": "file:///etc/passwd"})
        self.assertFalse(blocked.get("ok"))
        self.assertEqual(blocked.get("error"), "nav_forbidden")
        self.assertEqual(relay.ws.sent, [])

        allowed = relay.dispatch({"kind": "nav", "url": "https://www.stitchslop.com/"})
        self.assertTrue(allowed.get("ok"))
        methods = [m.get("method") for m in relay.ws.sent]
        self.assertIn("Page.navigate", methods)
        nav = next(m for m in relay.ws.sent if m.get("method") == "Page.navigate")
        self.assertEqual(nav["params"]["url"], "https://www.stitchslop.com/")


class TokenAndHostHelpers(unittest.TestCase):
    def setUp(self):
        self.browser = load("stitchslop_browser_helpers_test", "browser.py")

    def test_token_equal_rejects_empty_and_mismatch(self):
        token = "a" * 32
        self.assertTrue(self.browser.token_equal(token, token))
        self.assertFalse(self.browser.token_equal("", token))
        self.assertFalse(self.browser.token_equal(None, token))
        self.assertFalse(self.browser.token_equal(token, ""))
        self.assertFalse(self.browser.token_equal("b" * 32, token))
        self.assertFalse(self.browser.token_equal(token[:-1], token))

    def test_host_header_matches_bind_and_port(self):
        matches = self.browser.host_header_matches
        self.assertTrue(matches("127.0.0.1:8791", "127.0.0.1", 8791))
        self.assertFalse(matches("127.0.0.1", "127.0.0.1", 8791))
        self.assertFalse(matches("127.0.0.1:80", "127.0.0.1", 8791))
        self.assertFalse(matches("evil.example:8791", "127.0.0.1", 8791))
        self.assertFalse(matches("", "127.0.0.1", 8791))
        self.assertTrue(matches("[::1]:8792", "::1", 8792))
        self.assertFalse(matches("::1:8792", "::1", 8792))

    def test_origin_allowlist_drops_null_and_file(self):
        allowed = self.browser.ALLOWED_ORIGIN
        self.assertTrue(allowed.match("http://127.0.0.1:1234"))
        self.assertTrue(allowed.match("https://localhost"))
        self.assertTrue(allowed.match("app://hermes"))
        self.assertFalse(allowed.match("null"))
        self.assertFalse(allowed.match("file://"))
        self.assertFalse(allowed.match("file:///tmp/x.html"))
        self.assertFalse(allowed.match("https://evil.example"))


class _FakeRelay:
    def __init__(self, frame: bytes = b""):
        self.lock = threading.Lock()
        self.frame = frame
        self.frame_seq = 1 if frame else 0
        self.connected = True
        self.last_error = ""
        self.overlay = True
        self.dispatched: list[dict] = []

    def events_since(self, since: int):
        return []

    def dispatch(self, event: dict) -> dict:
        self.dispatched.append(event)
        return {"ok": True}


class RelayHTTP(unittest.TestCase):
    TOKEN = "unit-test-relay-token-value"

    def setUp(self):
        self.browser = load("stitchslop_browser_http_test", "browser.py")
        self.relay = _FakeRelay(frame=b"\xff\xd8fakejpeg")
        handler = self.browser.make_handler(self.relay, self.TOKEN)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _request(self, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, data, hdrs
        finally:
            conn.close()

    def test_viewer_requires_query_token(self):
        status, body, _ = self._request("GET", "/viewer")
        self.assertEqual(status, 401)
        self.assertNotIn(self.TOKEN.encode(), body)

        status, body, _ = self._request("GET", "/")
        self.assertEqual(status, 401)

        status, body, _ = self._request("GET", "/viewer?t=wrong-token")
        self.assertEqual(status, 401)
        self.assertNotIn(self.TOKEN.encode(), body)

        # A header alone is not enough to load the viewer HTML.
        status, body, _ = self._request(
            "GET", "/viewer", headers={"x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 401)

        status, body, _ = self._request("GET", f"/viewer?t={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn(b"Stitch Slop", body)
        self.assertIn(self.TOKEN.encode(), body)

    def test_ping_and_frame_require_token_and_never_echo_it(self):
        for path in ("/ping", "/frame.jpg"):
            status, body, _ = self._request("GET", path)
            self.assertEqual(status, 401, msg=path)
            self.assertNotIn(self.TOKEN.encode(), body)

        status, body, _ = self._request(
            "GET", "/ping", headers={"x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload.get("ok"))
        self.assertNotIn("token", payload)
        self.assertNotIn(self.TOKEN, body.decode())

        status, body, _ = self._request("GET", f"/frame.jpg?t={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\xff\xd8fakejpeg")

        status, body, _ = self._request("GET", "/ping?t=wrong")
        self.assertEqual(status, 401)

    def test_null_origin_does_not_receive_token_or_cors(self):
        status, body, hdrs = self._request(
            "GET", "/ping", headers={"Origin": "null"},
        )
        self.assertEqual(status, 401)
        self.assertNotIn(self.TOKEN.encode(), body)
        self.assertNotIn("access-control-allow-origin", hdrs)

        status, body, hdrs = self._request(
            "GET", "/ping",
            headers={"Origin": "file://", "x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("token", json.loads(body))
        self.assertNotIn("access-control-allow-origin", hdrs)

    def test_options_has_no_private_network_header(self):
        status, _, hdrs = self._request("OPTIONS", "/ping")
        self.assertEqual(status, 204)
        self.assertNotIn("access-control-allow-private-network", hdrs)

    def test_bad_host_is_rejected(self):
        status, body, _ = self._request(
            "GET", f"/viewer?t={self.TOKEN}",
            headers={"Host": f"evil.example:{self.port}"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body).get("error"), "bad_host")
        self.assertNotIn(self.TOKEN.encode(), body)

        status, body, _ = self._request(
            "GET", "/ping",
            headers={"Host": "127.0.0.1:80", "x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 403)

    def test_good_host_with_token_is_accepted(self):
        status, body, _ = self._request(
            "GET", "/ping",
            headers={"Host": f"127.0.0.1:{self.port}", "x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body).get("ok"))

    def test_events_and_input_still_token_gated(self):
        status, _, _ = self._request("GET", "/events?since=0")
        self.assertEqual(status, 401)
        status, body, _ = self._request(
            "GET", "/events?since=0", headers={"x-stitchslop-pane": self.TOKEN},
        )
        self.assertEqual(status, 200)

        status, _, _ = self._request(
            "POST", "/input",
            headers={"Content-Type": "application/json"},
            body=b'{"kind":"text","text":"x"}',
        )
        self.assertEqual(status, 401)
        status, body, _ = self._request(
            "POST", "/input",
            headers={"Content-Type": "application/json", "x-stitchslop-pane": self.TOKEN},
            body=b'{"kind":"text","text":"x"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.relay.dispatched[0]["kind"], "text")

    def test_ping_relay_helper_authenticates(self):
        payload = self.browser.ping_relay(self.port, self.TOKEN, timeout=2)
        self.assertTrue(payload.get("ok"))
        self.assertNotIn("token", payload)


class ConnectPairingGate(unittest.TestCase):
    def setUp(self):
        self.plugin = load("stitchslop_plugin_token_test", "__init__.py")

    def test_refuses_model_supplied_token(self):
        raw = self.plugin._connect({"token": "tok_fake_from_chat"})
        body = json.loads(raw)
        self.assertIsInstance(raw, str)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "unsupported_operation")
        self.assertEqual(body.get("code"), "pair_via_pane")
        self.assertIn("must not write the token file", body.get("fix", ""))

    def test_refuses_any_token_shape(self):
        raw = self.plugin._connect({"token": "not-even-tok"})
        body = json.loads(raw)
        self.assertEqual(body.get("error"), "unsupported_operation")
        self.assertEqual(body.get("code"), "pair_via_pane")

    def test_reconnect_without_token_calls_start_bridge(self):
        with mock.patch.object(self.plugin, "start_bridge", return_value={"ok": True, "connected": False}) as start:
            raw = self.plugin._connect({"wait_seconds": 5, "origin": "https://www.stitchslop.com"})
            body = json.loads(raw)
        start.assert_called_once_with(None, "https://www.stitchslop.com", wait_seconds=5)
        self.assertTrue(body.get("ok"))

    def test_status_still_string_json(self):
        with mock.patch.object(self.plugin, "_bridge_session", return_value=None):
            with mock.patch.object(self.plugin, "browser_mod") as browser:
                browser.return_value.status.return_value = {"ok": True, "chrome": False}
                raw = self.plugin._status({})
        body = json.loads(raw)
        self.assertIsInstance(raw, str)
        self.assertFalse(body.get("bridge"))


if __name__ == "__main__":
    unittest.main()
