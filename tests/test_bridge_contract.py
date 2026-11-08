"""Contract tests for the bridge, run against the real server on a loopback port.

These lock in the failures that cost real debugging time:
  * the app sends its tool list as ``{"tools": [...], "id": null}`` — a parser that
    treats any ``id`` key as a reply caches nothing and leaves ``tools`` empty;
  * a bootstrap token is one-time and must be refused on a second handshake;
  * the origin allowlist and the control-plane key are the only two boundaries.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://www.stitchslop.com"


def load_bridge():
    spec = importlib.util.spec_from_file_location("ss_bridge_under_test", ROOT / "bridge" / "stitchslop_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_bridge()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    """Minimal masked WebSocket client — the browser side of the handshake."""

    def __init__(self, port: int, origin: str = ORIGIN):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
             f"Origin: {origin}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        self.status = head.split(b"\r\n")[0].decode(errors="replace") if head else ""
        self.buf = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""

    def send(self, obj) -> None:
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack("!H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(head + mask + bytes(c ^ mask[i & 3] for i, c in enumerate(payload)))

    def recv(self, timeout: float = 5.0):
        self.sock.settimeout(timeout)
        while len(self.buf) < 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        n = self.buf[1] & 0x7F
        offset = 2
        if n == 126:
            while len(self.buf) < 4:
                self.buf += self.sock.recv(65536)
            n = struct.unpack("!H", self.buf[2:4])[0]
            offset = 4
        while len(self.buf) < offset + n:
            self.buf += self.sock.recv(65536)
        payload = self.buf[offset:offset + n]
        self.buf = self.buf[offset + n:]
        return json.loads(payload.decode() or "{}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class BridgeFixture:
    """A real bridge on a real port, torn down afterwards."""

    def __init__(self, token: str):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.bridge = B.Bridge([ORIGIN], Path(self.tmp.name), self.port, 0, token)
        self.bridge.bind()
        threading.Thread(target=self.bridge.serve, daemon=True).start()
        time.sleep(0.3)

    def handshake(self, token: str) -> dict:
        client = Client(self.port)
        assert "101" in client.status, client.status
        client.send({"token": token})
        reply = client.recv()
        client.close()
        return reply

    def close(self) -> None:
        self.bridge.stop.set()
        try:
            if self.bridge.sock:
                self.bridge.sock.close()
        except OSError:
            pass
        self.tmp.cleanup()


class BridgeContract(unittest.TestCase):
    def setUp(self):
        self.fx = BridgeFixture("tok_contract")

    def tearDown(self):
        self.fx.close()

    def test_token_handshake_mints_pairing(self):
        reply = self.fx.handshake("tok_contract")
        self.assertEqual(reply.get("hello"), "stitchslop-connector")
        self.assertEqual(reply.get("protocol"), 1)
        self.assertTrue(reply.get("pairingSecret"), "a token handshake must return a pairing secret")

    def test_token_is_one_time(self):
        self.fx.handshake("tok_contract")
        self.assertEqual(self.fx.handshake("tok_contract").get("error"), "bad_credential")

    def test_tool_list_with_null_id_is_cached(self):
        """Regression: the app sends {"tools": [...], "id": null}, not a bare list."""
        client = Client(self.fx.port)
        client.send({"token": "tok_contract"})
        self.assertEqual(client.recv().get("hello"), "stitchslop-connector")
        client.send({"tools": [{"name": "scene.describe"}, {"name": "scene.render"}], "id": None})
        deadline = time.time() + 3
        while time.time() < deadline and len(self.fx.bridge.tools) < 2:
            time.sleep(0.05)
        self.assertEqual([t["name"] for t in self.fx.bridge.tools],
                         ["scene.describe", "scene.render"])
        client.close()

    def test_foreign_origin_is_refused(self):
        client = Client(self.fx.port, origin="https://evil.example")
        self.assertIn("403", client.status)
        client.close()

    def test_control_plane_needs_the_key(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.fx.port}/status", timeout=3)
        self.assertEqual(ctx.exception.code, 401)

    def test_control_plane_answers_with_key(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.fx.port}/status",
                                     headers={"x-stitchslop-key": self.fx.bridge.key})
        with urllib.request.urlopen(req, timeout=3) as fh:
            body = json.loads(fh.read())
        self.assertTrue(body["ok"])
        self.assertIn("connected", body)

    def test_control_plane_refuses_a_page_origin(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.fx.port}/status",
                                     headers={"x-stitchslop-key": self.fx.bridge.key,
                                              "Origin": ORIGIN})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
