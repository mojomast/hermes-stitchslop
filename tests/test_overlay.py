"""Fancy-mode overlay: event feed, pacing contract, persistence and tool schema.

The relay is the one choke point every forwarded action passes through, so its
ring buffer is what the viewer animates. These tests pin the parts a UI cannot
prove by itself: monotonic ids, order, click de-duplication, token gating, and
the overlay flag surviving in the browser state file.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_module("stitchslop_browser_under_test", "browser.py")
P = load_module("stitchslop_plugin_under_test", "__init__.py")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeWS:
    """Accepts CDP sends so dispatch() succeeds without a browser."""

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, obj: dict) -> None:
        self.sent.append(obj)


class RelayFixture:
    def __init__(self, token: str = "tok_overlay_test"):
        self.port = free_port()
        self.token = token
        self.relay = B.Relay()
        self.relay.ws = FakeWS()
        self.relay.connected = True
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port),
                                         B.make_handler(self.relay, token))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def post(self, payload: dict, route: str = "/input", token: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-stitchslop-pane"] = self.token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}", data=json.dumps(payload).encode(),
            headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=3) as fh:
            return json.loads(fh.read())

    def get(self, route: str, token: bool = True):
        headers = {"x-stitchslop-pane": self.token} if token else {}
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{route}",
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=3) as fh:
            return json.loads(fh.read())

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Events(unittest.TestCase):
    def setUp(self):
        self.fx = RelayFixture()

    def tearDown(self):
        self.fx.close()

    def test_events_return_actions_in_order_with_ids(self):
        self.fx.post({"kind": "mouse", "action": "down", "x": 120, "y": 240, "button": "left"})
        self.fx.post({"kind": "text", "text": "hoop"})
        self.fx.post({"kind": "key", "key": "Enter", "code": "Enter", "vk": 13})

        events = self.fx.get("/events?since=0")
        self.assertIsInstance(events, list)
        self.assertEqual([e["id"] for e in events], [1, 2, 3])
        self.assertEqual([e["kind"] for e in events], ["click", "text", "key"])
        self.assertEqual((events[0]["x"], events[0]["y"]), (120, 240))
        self.assertEqual(events[1]["text"], "hoop")
        self.assertEqual(events[2]["key"], "Enter")
        self.assertTrue(all("ts" in e for e in events))

    def test_since_is_an_exclusive_cursor(self):
        for n in range(4):
            self.fx.post({"kind": "mouse", "action": "down", "x": n, "y": n})
        events = self.fx.get("/events?since=2")
        self.assertEqual([e["id"] for e in events], [3, 4])
        self.assertEqual(self.fx.get("/events?since=4"), [])

    def test_events_need_the_pane_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fx.get("/events?since=0", token=False)
        self.assertEqual(ctx.exception.code, 401)

    def test_one_event_per_click_not_per_pointer_phase(self):
        self.fx.post({"kind": "mouse", "action": "down", "x": 10, "y": 10})
        self.fx.post({"kind": "mouse", "action": "move", "x": 40, "y": 40})
        self.fx.post({"kind": "mouse", "action": "up", "x": 40, "y": 40})
        events = self.fx.get("/events?since=0")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "click")

    def test_python_callers_hit_the_same_feed(self):
        """Tools (or a future Python driver) dispatch in-process; still visible."""
        result = self.fx.relay.dispatch({"kind": "text", "text": "from python"})
        self.assertTrue(result["ok"])
        events = self.fx.get("/events?since=0")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "text")
        self.assertEqual(events[0]["text"], "from python")

    def test_failed_dispatch_is_not_animated(self):
        self.fx.relay.ws = None
        self.assertFalse(self.fx.relay.dispatch({"kind": "text", "text": "lost"})["ok"])
        self.assertEqual(self.fx.get("/events?since=0"), [])

    def test_overlay_endpoint_updates_ping(self):
        self.assertTrue(self.fx.get("/ping")["overlay"])
        self.fx.post({"enabled": False}, route="/overlay")
        self.assertFalse(self.fx.get("/ping")["overlay"])
        self.fx.post({"enabled": True}, route="/overlay")
        self.assertTrue(self.fx.get("/ping")["overlay"])

    def test_viewer_serves_effects_and_event_poll(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.fx.port}/viewer?t={self.fx.token}")
        with urllib.request.urlopen(request, timeout=3) as fh:
            html = fh.read().decode()
        self.assertIn('id="fx"', html)
        self.assertIn('id="fancy"', html)
        self.assertIn("/events?since=", html)
        self.assertIn(f'const TOKEN = "{self.fx.token}"', html)
        self.assertNotIn("__TOKEN__", html)


class OverlayState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = B.STATE_DIR
        B.STATE_DIR = Path(self.tmp.name) / "pane"

    def tearDown(self):
        B.STATE_DIR = self._old
        self.tmp.cleanup()

    def test_flag_round_trips_through_state(self):
        self.assertTrue(B.overlay_enabled(), "fancy mode defaults on")
        result = B.set_overlay(False)
        self.assertEqual(result["overlay"], False)
        self.assertFalse(B.overlay_enabled())
        self.assertEqual(B.read_state()["overlay"], False)
        B.set_overlay(True)
        self.assertTrue(B.overlay_enabled())
        self.assertEqual(B.read_state()["overlay"], True)

    def test_state_file_is_private(self):
        B.set_overlay(False)
        path = B.state_path()
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_status_reports_the_flag(self):
        B.set_overlay(False)
        self.assertFalse(B.status()["overlay"])

    def test_stop_keeps_a_deliberate_choice(self):
        B.set_overlay(False)
        B.write_state({"overlay": False})
        B.stop()
        self.assertFalse(B.overlay_enabled(), "a stop/start cycle must remember the choice")


class Schema(unittest.TestCase):
    def test_existing_arguments_are_unchanged_and_overlay_is_optional(self):
        params = P.BROWSER_SCHEMA["parameters"]
        self.assertEqual(params["properties"]["action"]["enum"], ["start", "stop", "status"])
        self.assertEqual(params["required"], ["action"])
        self.assertFalse(params["additionalProperties"])
        overlay = params["properties"]["overlay"]
        self.assertEqual(overlay["type"], "boolean")
        self.assertIs(overlay["default"], True)
        self.assertNotIn("overlay", params["required"], "omitting it must keep the stored value")

    def test_tool_surface_is_still_four(self):
        class Ctx:
            def __init__(self):
                self.tools = []

            def register_skill(self, name, path):
                pass

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

        ctx = Ctx()
        P.register(ctx)
        self.assertEqual([t["name"] for t in ctx.tools],
                         ["stitchslop_status", "stitchslop_connect",
                          "stitchslop_call", "stitchslop_browser"])
        self.assertIs(ctx.tools[3]["schema"], P.BROWSER_SCHEMA)

    def test_browser_tool_passes_overlay_to_the_browser_module(self):
        calls = []

        class Stub:
            def ensure_relay(self, overlay=None):
                calls.append(("ensure", overlay))
                return {"ok": True, "overlay": overlay}

            def set_overlay(self, value):
                calls.append(("set", value))
                return {"ok": True, "overlay": value}

            def status(self):
                return {"ok": True, "overlay": calls[-1][1]}

            def stop(self):
                return {"ok": True}

        old = P.browser_mod
        P.browser_mod = lambda: Stub()
        try:
            started = json.loads(P._browser({"action": "start", "overlay": False}))
            self.assertFalse(started["overlay"])
            self.assertEqual(calls, [("ensure", False)])
            json.loads(P._browser({"action": "status", "overlay": True}))
            self.assertEqual(calls[-1], ("set", True))
            json.loads(P._browser({"action": "start"}))
            self.assertEqual(calls[-1], ("ensure", None), "omitting overlay must not reset it")
        finally:
            P.browser_mod = old


if __name__ == "__main__":
    unittest.main()
