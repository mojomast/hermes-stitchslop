"""Catalog-readiness: loopback-only default bind and token shape."""
from __future__ import annotations

import importlib.util
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


class ConnectToken(unittest.TestCase):
    def setUp(self):
        self.plugin = load("stitchslop_plugin_token_test", "__init__.py")

    def test_rejects_non_tok_prefix(self):
        import json
        raw = self.plugin._connect({"token": "not-a-panel-token"})
        body = json.loads(raw)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "bad_token_shape")


if __name__ == "__main__":
    unittest.main()
