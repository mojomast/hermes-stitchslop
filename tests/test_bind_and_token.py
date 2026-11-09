"""Catalog-readiness: loopback-only default bind and pane-only pairing."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
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

    def test_override_list(self):
        with mock.patch.dict(os.environ, {"STITCHSLOP_PANE_BIND": "100.64.0.1"}):
            self.assertEqual(self.browser.bind_hosts(), ["100.64.0.1"])


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
