"""Envelope persistence: every base64 payload an app reply carries must reach disk.

The bug this locks in: `design.export` answers with a `files` list where each entry
carries its own `dataUrl`. Handling only a top-level `dataUrl` wrote nothing and
reported success — the exported machine files silently vanished.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("stitchslop_plugin_under_test", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Persist(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.plugin.ASSETS = base / "pane"
        self.plugin.EXPORTS = base / "exports"
        self.plugin.ASSETS.mkdir(parents=True)
        self.plugin.EXPORTS.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_render_is_saved(self):
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\nraw").decode()
        reply = {"ok": True, "say": "Rendered.", "dataUrl": f"data:image/png;base64,{payload}"}
        self.plugin._persist(reply)
        self.assertNotIn("dataUrl", reply, "base64 must not stay in the envelope")
        saved = Path(reply["savedTo"])
        self.assertTrue(saved.exists())
        self.assertEqual(saved.read_bytes(), b"\x89PNG\r\n\x1a\nraw")
        self.assertEqual(reply["savedBytes"], len(b"\x89PNG\r\n\x1a\nraw"))

    def test_export_files_list_is_saved(self):
        dst = base64.b64encode(b"DSTDATA").decode()
        col = base64.b64encode(b"COLDATA").decode()
        reply = {
            "ok": True,
            "files": [
                {"name": "digitize.dst", "bytes": 7, "dataUrl": f"data:application/octet-stream;base64,{dst}"},
                {"name": "digitize.col", "bytes": 7, "dataUrl": f"data:application/octet-stream;base64,{col}"},
            ],
        }
        self.plugin._persist(reply)
        self.assertEqual(len(reply["savedFiles"]), 2)
        for entry in reply["files"]:
            self.assertNotIn("dataUrl", entry)
            self.assertTrue(Path(entry["savedTo"]).exists())
        self.assertEqual((self.plugin.EXPORTS / "digitize.dst").read_bytes(), b"DSTDATA")
        self.assertEqual((self.plugin.EXPORTS / "digitize.col").read_bytes(), b"COLDATA")

    def test_path_traversal_in_export_name_is_neutralised(self):
        payload = base64.b64encode(b"x").decode()
        reply = {"ok": True, "files": [{"name": "../../evil.bin",
                                        "dataUrl": f"data:application/octet-stream;base64,{payload}"}]}
        self.plugin._persist(reply)
        saved = Path(reply["files"][0]["savedTo"])
        self.assertEqual(saved.parent, self.plugin.EXPORTS, "must be written inside the exports dir")
        self.assertFalse((Path(self.tmp.name).parent / "evil.bin").exists())

    def test_no_payload_is_a_no_op(self):
        reply = {"ok": True, "changed": False, "say": "no-op"}
        self.plugin._persist(reply)
        self.assertNotIn("savedTo", reply)
        self.assertEqual(list(self.plugin.EXPORTS.iterdir()), [])

    def test_handler_returns_a_json_string(self):
        out = self.plugin._status({})
        self.assertIsInstance(out, str)
        self.assertIn("bridge", json.loads(out))


if __name__ == "__main__":
    unittest.main()
