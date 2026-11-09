#!/usr/bin/env python3
"""Stitch Slop bridge — a loopback WebSocket + HTTP control plane for the app's tab.

Implements the protocol published at https://www.stitchslop.com/agent (protocol 1)
in the Python standard library only: no third-party dependencies, no network
access beyond 127.0.0.1, and no browser is ever opened from here.

Faces:
  * WebSocket server on 127.0.0.1:8787-8790 — the user's Stitch Slop tab dials in.
  * HTTP control plane on the same port — /status /tools /wait /call, keyed by
    the 0600 session file, for shells and agents.
  * CLI: `status | tools | wait [S] | call [JSON] [--out FILE]`.

MCP over stdio is deliberately not implemented: Hermes registers native tools and
drives the control plane, which is the path the protocol calls §4.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import hmac
import json
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL = 1
PORTS = (8787, 8788, 8789, 8790)
DEFAULT_ORIGINS = ("https://www.stitchslop.com", "https://beta.stitchslop.com")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEAD = 64 * 1024
HEARTBEAT_SECONDS = 30
DEAD_AFTER_SECONDS = 90
UNPAIRED_IDLE_SECONDS = 30 * 60
CALL_TIMEOUT = 120.0


def log(msg: str) -> None:
    """Diagnostics go to stderr — stdout belongs to the JSON envelope."""
    print(f"[stitchslop-bridge] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def url_safe_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- ws
def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return bytes(buf)


def ws_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """Return (opcode, payload). Client frames must be masked (RFC 6455 §5.1)."""
    b1, b2 = _read_exact(sock, 2)
    fin = b1 & 0x80
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]
    if length > 32 * 1024 * 1024:
        raise ConnectionError("frame too large")
    mask = _read_exact(sock, 4) if masked else b""
    payload = _read_exact(sock, length) if length else b""
    if masked and mask:
        payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
    if not fin and opcode in (0x1, 0x2):
        # Continuation frames: keep reading until FIN.
        parts = [payload]
        while True:
            b1n, b2n = _read_exact(sock, 2)
            ln = b2n & 0x7F
            op = b1n & 0x0F
            if ln == 126:
                ln = struct.unpack("!H", _read_exact(sock, 2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", _read_exact(sock, 8))[0]
            m = _read_exact(sock, 4) if (b2n & 0x80) else b""
            part = _read_exact(sock, ln) if ln else b""
            if m:
                part = bytes(c ^ m[i & 3] for i, c in enumerate(part))
            parts.append(part)
            if b1n & 0x80:
                break
            if op == 0x8:
                raise ConnectionError("close")
        payload = b"".join(parts)
    return opcode, payload


def ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    sock.sendall(bytes(header) + payload)


def ws_send_json(sock: socket.socket, obj: Any) -> None:
    ws_send(sock, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


# ------------------------------------------------------------------ bridge state
class Bridge:
    def __init__(self, origins: list[str], config_dir: Path, port_hint: int | None,
                 agent_idle_minutes: float, token: str | None) -> None:
        self.origins = origins
        self.config_dir = config_dir
        self.port_hint = port_hint
        self.agent_idle_minutes = agent_idle_minutes
        self.token = (token or "").strip() or None
        self.lock = threading.RLock()
        self.sock: socket.socket | None = None
        self.port: int | None = None
        self.page: socket.socket | None = None
        self.page_origin: str | None = None
        self.page_addr: str | None = None
        self.tools: list[dict] = []
        self.connected = False
        self.handshake_at: float | None = None
        self.last_rx = 0.0
        self.sent_any = False
        self.next_id = 0
        self.pending: dict[int, queue.Queue] = {}
        self.stop = threading.Event()
        self.page_ready = threading.Event()
        self.key = url_safe_token(24)
        self.token_file = config_dir / "pairing.json"
        self.started = time.time()
        self.last_agent_activity = time.time()
        self.stop_reason = ""

    # ---------------------------------------------------------------- pairing
    def _load_pairing(self) -> dict:
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_pairing(self, data: dict) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_file)

    def paired_for(self, origin: str) -> bool:
        return bool(self._load_pairing().get("origins", {}).get(origin, {}).get("secretHash"))

    def check_secret(self, origin: str, secret: str) -> bool:
        stored = self._load_pairing().get("origins", {}).get(origin, {}).get("secretHash")
        if not stored or not secret:
            return False
        return hmac.compare_digest(stored, sha256(secret))

    def redeem_token(self, origin: str, token: str) -> tuple[bool, str]:
        """Spend the one-time token. Returns (ok, secret-or-refusal)."""
        data = self._load_pairing()
        digest = sha256(token)
        for _origin_key, listed in (data.get("redeemed") or {}).items():
            if digest in listed:
                return False, "already redeemed"
        entry = data.setdefault("origins", {}).setdefault(origin, {})
        minted = url_safe_token(32)
        entry["secretHash"] = sha256(minted)
        entry["pairedAt"] = now_iso()
        data.setdefault("redeemed", {}).setdefault(origin, []).append(digest)
        self._save_pairing(data)
        return True, minted

    def token_redeemed(self, token: str) -> bool:
        digest = sha256(token)
        for listed in (self._load_pairing().get("redeemed") or {}).values():
            if digest in listed:
                return True
        return False

    # -------------------------------------------------------------- lifecycle
    def session_path(self, port: int) -> Path:
        return self.config_dir / f"session-{port}.json"

    def write_session(self) -> None:
        assert self.port
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.config_dir, 0o700)
        except OSError:
            pass
        path = self.session_path(self.port)
        body = {
            "port": self.port,
            "key": self.key,
            "origin": self.origins[0] if self.origins else "",
            "origins": self.origins,
            "pid": os.getpid(),
            "protocol": PROTOCOL,
            "startedAt": now_iso(),
        }
        path.write_text(json.dumps(body, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)

    def remove_session(self) -> None:
        if not self.port:
            return
        path = self.session_path(self.port)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def bind(self) -> None:
        candidates = [self.port_hint] if self.port_hint else list(PORTS)
        last: Exception | None = None
        for port in candidates:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                s.listen(8)
                self.sock, self.port = s, port
                break
            except OSError as exc:
                last = exc
                s.close()
        if not self.sock:
            log(f"no free port in {candidates}: {last}")
            raise SystemExit(2)
        try:
            import fcntl  # noqa: F401  (guarded: not on Windows)
        except ImportError:
            pass
        self.write_session()
        log(f"listening on 127.0.0.1:{self.port} for {','.join(self.origins)} pid={os.getpid()}")

    def serve(self) -> None:
        assert self.sock
        self.sock.settimeout(0.5)
        threading.Thread(target=self._heartbeat, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._idle_watchdog, name="idle", daemon=True).start()
        while not self.stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _idle_watchdog(self) -> None:
        while not self.stop.wait(5):
            now = time.time()
            if self.handshake_at is None and now - self.started > UNPAIRED_IDLE_SECONDS:
                self.stop_reason = "no tab connected within 30 minutes"
                self.stop.set()
                break
            if self.handshake_at is not None and self.agent_idle_minutes > 0:
                if now - self.last_agent_activity > self.agent_idle_minutes * 60:
                    self.stop_reason = f"no agent activity for {self.agent_idle_minutes:g} minutes"
                    self.stop.set()
                    break
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _heartbeat(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            with self.lock:
                page = self.page
                last = self.last_rx
            if not page:
                continue
            try:
                ws_send(page, b"", opcode=0x9)
            except OSError:
                self._page_gone("ping failed")
                continue
            if time.time() - last > DEAD_AFTER_SECONDS:
                self._page_gone("heartbeat timeout")

    def _page_gone(self, why: str) -> None:
        with self.lock:
            page, self.page = self.page, None
            self.connected = False
            self.tools = []
            pending, self.pending = self.pending, {}
        if page:
            try:
                page.close()
            except OSError:
                pass
        for q in pending.values():
            q.put({"ok": False, "error": "page_gone", "message": f"the tab went away ({why})"})
        log(f"page gone: {why}")

    # ------------------------------------------------------------------- conn
    def _handle(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        try:
            head = b""
            conn.settimeout(15)
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                head += chunk
                if len(head) > MAX_HEAD:
                    conn.close()
                    return
            raw_head, rest = head.split(b"\r\n\r\n", 1)
            lines = raw_head.decode("latin-1").split("\r\n")
            method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            upgrade = headers.get("upgrade", "").lower()
            if upgrade == "websocket":
                self._serve_ws(conn, headers, rest)
                return
            self._serve_http(conn, method, path, headers, rest)
        except (ConnectionError, OSError, ValueError) as exc:
            log(f"connection from {addr} ended: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_ws(self, conn: socket.socket, headers: dict, rest: bytes) -> None:
        origin = headers.get("origin", "")
        if not self._origin_ok(origin):
            log(f"refused upgrade: origin {origin!r} not allowed")
            body = b'{"error":"forbidden_origin"}'
            conn.sendall(
                b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            return
        key = headers.get("sec-websocket-key", "")
        if not key or headers.get("sec-websocket-version") != "13":
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + ws_accept(key).encode() + b"\r\n\r\n"
        )
        if rest:
            # The client pipelined its first frame with the upgrade request.
            buf = _Reader(conn, rest)
        else:
            buf = _Reader(conn, b"")

        # First frame must carry the credential.
        conn.settimeout(20)
        try:
            opcode, payload = buf.read_frame()
        except (ConnectionError, socket.timeout):
            return
        if opcode != 0x1:
            return
        try:
            hello = json.loads(payload.decode("utf-8"))
        except ValueError:
            ws_send_json(conn, {"error": "bad_credential", "message": "first frame was not JSON"})
            return
        token = str(hello.get("token") or "")
        secret = str(hello.get("secret") or "")
        accepted_by_secret = False
        pairing_secret = None
        if secret and self.check_secret(origin, secret):
            accepted_by_secret = True
        elif token:
            matches = bool(self.token) and hmac.compare_digest(token, self.token)
            if matches and not self.token_redeemed(token):
                ok, minted = self.redeem_token(origin, token)
                if not ok:
                    ws_send_json(conn, {"error": "bad_credential",
                                        "message": "that token has already been used — reopen the Agent panel for a fresh one"})
                    return
                pairing_secret = minted
            elif matches:
                ws_send_json(conn, {"error": "bad_credential",
                                    "message": "that token has already been used — they are one-time. "
                                               "Reopen \"⌁ Agent\" in the app for a fresh one."})
                return
        if not accepted_by_secret and not pairing_secret:
            hint = ("that token is not the one this connector was started with"
                    if self.token else "this connector was started without a token and has no pairing for this origin")
            ws_send_json(conn, {"error": "bad_credential", "message": hint})
            return

        with self.lock:
            incumbent = self.page
            alive = False
            if incumbent is not None:
                try:
                    ws_send(incumbent, b"", opcode=0x9)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                ws_send_json(conn, {"error": "busy", "busy": True,
                                    "message": "This connector is already serving another Stitch Slop tab. "
                                               "Close the other tab, or start another bridge."})
                return
            if incumbent is not None:
                try:
                    ws_send_json(incumbent, {"displaced": True,
                                             "message": "Another Stitch Slop tab took this connection."})
                    incumbent.close()
                except OSError:
                    pass
            self.page = conn
            self.page_origin = origin
            self.page_addr = f"{self.page_addr or ''}"
            self.connected = True
            self.handshake_at = time.time()
            self.last_rx = time.time()
            self.tools = []
        self.page_ready.set()
        hello_frame = {"hello": "stitchslop-connector", "protocol": PROTOCOL, "port": self.port}
        if pairing_secret:
            hello_frame["pairingSecret"] = pairing_secret
        log(f"tab attached from {origin} ({'paired' if accepted_by_secret else 'new pairing'})")
        ws_send_json(conn, hello_frame)

        conn.settimeout(None)
        try:
            while not self.stop.is_set():
                opcode, payload = buf.read_frame()
                with self.lock:
                    self.last_rx = time.time()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    try:
                        ws_send(conn, payload, opcode=0xA)
                    except OSError:
                        break
                    continue
                if opcode == 0xA:
                    continue
                if opcode != 0x1:
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(msg, dict) and "tools" in msg and msg.get("id") is None:
                    with self.lock:
                        self.tools = list(msg.get("tools") or [])
                    log(f"tool list: {len(self.tools)} commands")
                    continue
                mid = msg.get("id") if isinstance(msg, dict) else None
                if mid is not None:
                    with self.lock:
                        waiter = self.pending.pop(int(mid), None)
                    if waiter:
                        waiter.put(msg)
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            with self.lock:
                if self.page is conn:
                    self.page = None
                    self.connected = False
                    self.tools = []
                    pending, self.pending = self.pending, {}
                else:
                    pending = {}
            for q in pending.values():
                q.put({"ok": False, "error": "page_gone", "message": "the tab disconnected"})
            log("tab detached")

    # ------------------------------------------------------------- http plane
    def _serve_http(self, conn: socket.socket, method: str, path: str,
                    headers: dict[str, str], rest: bytes = b"") -> None:
        body_len = int(headers.get("content-length") or 0)
        body = rest
        if len(body) < body_len:
            conn.settimeout(15)
            body += _read_exact(conn, body_len - len(body))
        body = body[:body_len]

        def respond(code: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            conn.sendall(
                f"HTTP/1.1 {code} {_reason(code)}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
            )

        if "origin" in headers:
            respond(403, {"ok": False, "error": "origin_header",
                          "message": "control-plane requests must not carry an Origin header"})
            return
        if not hmac.compare_digest(headers.get("x-stitchslop-key", ""), self.key):
            respond(401, {"ok": False, "error": "unauthorized", "message": "bad or missing x-stitchslop-key"})
            return
        self.last_agent_activity = time.time()
        route = path.split("?")[0].rstrip("/") or "/"
        try:
            if route == "/status" and method == "GET":
                with self.lock:
                    respond(200, {
                        "ok": True, "connected": self.connected, "port": self.port,
                        "origin": self.page_origin or (self.origins[0] if self.origins else ""),
                        "protocol": PROTOCOL, "pid": os.getpid(),
                        "paired": bool(self.page_origin and self.paired_for(self.page_origin)),
                        "tools": [t.get("name") for t in self.tools],
                        "message": "connected" if self.connected else "waiting for a tab to dial in",
                    })
                return
            if route == "/tools" and method == "GET":
                with self.lock:
                    if not self.connected:
                        respond(200, {"ok": False, "error": "no_tab",
                                      "message": "no Stitch Slop tab is attached"})
                    else:
                        respond(200, {"ok": True, "tools": self.tools})
                return
            if route == "/wait" and method == "POST":
                seconds = 90.0
                if body:
                    try:
                        seconds = float(json.loads(body).get("seconds", seconds))
                    except (ValueError, AttributeError, TypeError):
                        seconds = 90.0
                seconds = max(0.0, min(seconds, 900.0))
                deadline = time.time() + seconds
                while time.time() < deadline:
                    with self.lock:
                        if self.connected:
                            break
                    time.sleep(0.2)
                with self.lock:
                    respond(200, {
                        "ok": self.connected,
                        "connected": self.connected,
                        "error": None if self.connected else "timeout",
                        "message": "tab attached" if self.connected else f"no tab within {seconds:g}s",
                        "tools": [t.get("name") for t in self.tools] if self.connected else [],
                    })
                return
            if route == "/call" and method == "POST":
                try:
                    req = json.loads(body or b"{}")
                except ValueError:
                    respond(400, {"ok": False, "error": "bad_json", "message": "body must be JSON"})
                    return
                command = str(req.get("command") or "")
                args = req.get("args") or {}
                timeout = float(req.get("timeout") or CALL_TIMEOUT)
                with self.lock:
                    page = self.page
                    if not self.connected or page is None:
                        respond(200, {"ok": False, "error": "no_tab",
                                      "message": "no Stitch Slop tab is attached"})
                        return
                    self.next_id += 1
                    mid = self.next_id
                    waiter: queue.Queue = queue.Queue()
                    self.pending[mid] = waiter
                try:
                    ws_send_json(page, {"id": mid, "command": command, "args": args})
                except OSError:
                    with self.lock:
                        self.pending.pop(mid, None)
                    respond(200, {"ok": False, "error": "page_gone",
                                  "message": "the tab disconnected before the command went out"})
                    return
                try:
                    reply = waiter.get(timeout=timeout)
                except queue.Empty:
                    with self.lock:
                        self.pending.pop(mid, None)
                    respond(504, {"ok": False, "error": "timeout",
                                  "message": f"the tab did not answer {command} within {timeout:g}s"})
                    return
                reply.pop("id", None)
                respond(200, reply)
                return
            respond(404, {"ok": False, "error": "not_found", "message": f"no route {route}"})
        except Exception as exc:  # noqa: BLE001 - control plane must never kill the bridge
            log(f"control-plane error: {type(exc).__name__}: {exc}")
            try:
                respond(500, {"ok": False, "error": "internal", "message": str(exc)})
            except OSError:
                pass

    def _origin_ok(self, origin: str) -> bool:
        candidate = (origin or "").strip().rstrip("/").lower()
        if not candidate or candidate == "null":
            return False
        return candidate in {o.rstrip("/").lower() for o in self.origins}


class _Reader:
    """Buffered reader so a first frame pipelined with the HTTP upgrade is kept."""

    def __init__(self, sock: socket.socket, buffered: bytes) -> None:
        self.sock = sock
        self.buf = bytearray(buffered)

    def _fill(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("eof")
            self.buf += chunk

    def _take(self, n: int) -> bytes:
        self._fill(n)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_frame(self) -> tuple[int, bytes]:
        b1, b2 = self._take(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._take(8))[0]
        if length > 32 * 1024 * 1024:
            raise ConnectionError("frame too large")
        mask = self._take(4) if masked else b""
        payload = self._take(length) if length else b""
        if mask:
            payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        return opcode, payload


def _reason(code: int) -> str:
    return {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            404: "Not Found", 426: "Upgrade Required", 500: "Internal Server Error",
            504: "Gateway Timeout"}.get(code, "OK")


# ------------------------------------------------------------------------ client
def _session_files(config_dir: Path) -> list[dict]:
    out = []
    for path in sorted(config_dir.glob("session-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = data.get("pid")
        try:
            if isinstance(pid, int) and pid > 0:
                os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                continue
        data["_path"] = str(path)
        out.append(data)
    return out


def pick_session(config_dir: Path, port: int | None) -> dict:
    live = _session_files(config_dir)
    if port:
        for item in live:
            if int(item.get("port") or 0) == port:
                return item
        print(f"no bridge running on port {port}", file=sys.stderr)
        raise SystemExit(3)
    if not live:
        print("no running Stitch Slop bridge — start one, or ask Hermes to connect", file=sys.stderr)
        raise SystemExit(3)
    if len(live) > 1:
        ports = ", ".join(str(i.get("port")) for i in live)
        print(f"several bridges are running on ports {ports}; pass --port N", file=sys.stderr)
        raise SystemExit(3)
    return live[0]


def control(session: dict, method: str, route: str, payload: dict | None = None,
            timeout: float = 300.0) -> tuple[int, dict]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", int(session["port"]), timeout=timeout)
    headers = {"x-stitchslop-key": session["key"], "Content-Type": "application/json"}
    body = json.dumps(payload).encode() if payload is not None else None
    conn.request(method, route, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, {"ok": False, "error": "bad_reply", "message": raw[:200].decode("latin-1")}


def cli(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir).expanduser()
    if args.command == "status":
        session = pick_session(config_dir, args.port)
        status, payload = control(session, "GET", "/status")
        payload.setdefault("ok", status == 200)
        print(json.dumps(payload))
        return 0 if payload.get("ok") else 1
    if args.command == "tools":
        session = pick_session(config_dir, args.port)
        _, payload = control(session, "GET", "/tools")
        print(json.dumps(payload))
        return 0 if payload.get("ok") else 1
    if args.command == "wait":
        session = pick_session(config_dir, args.port)
        seconds = float(args.seconds or 90)
        _, payload = control(session, "POST", "/wait", {"seconds": seconds}, timeout=seconds + 30)
        print(json.dumps(payload))
        return 0 if payload.get("ok") else 1
    if args.command == "call":
        session = pick_session(config_dir, args.port)
        try:
            request = json.loads(args.json_args or "{}")
        except ValueError as exc:
            print(f"usage: call '<json>' — {exc}", file=sys.stderr)
            return 64
        if "command" not in request:
            print("usage: call '{\"command\": \"scene.describe\", \"args\": {}}'", file=sys.stderr)
            return 64
        _, payload = control(session, "POST", "/call", request)
        data_url = payload.pop("dataUrl", None)
        if data_url and args.out:
            header, _, b64 = data_url.partition(",")
            data = base64.b64decode(b64)
            Path(args.out).expanduser().write_bytes(data)
            payload["savedTo"] = str(Path(args.out).expanduser())
            payload["savedBytes"] = len(data)
            payload["savedType"] = header.split(";")[0].split(":")[-1]
        elif data_url:
            print("reply carried an image; pass --out FILE to save it (kept out of stdout)", file=sys.stderr)
            payload.pop("dataUrl", None)
        print(json.dumps(payload))
        return 0 if payload.get("ok") else 1
    return 64


def read_token(token_file: str | None) -> str | None:
    """The bootstrap token from a 0600 ``--token-file`` only.

    Hermes and this plugin hand the token as a file so it never lands in a child
    process environment. Process-environment token injection is unsupported.
    """
    if not token_file:
        return None
    path = Path(token_file).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log(f"cannot read --token-file {path}: {exc}")
        return None
    if value:
        return value
    log(f"--token-file {path} is empty")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stitchslop-bridge", add_help=True)
    parser.add_argument("--origin", default=",".join(DEFAULT_ORIGINS),
                        help="allowed page origins, comma separated")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config-dir", default=str(Path.home() / ".stitchslop"))
    parser.add_argument("--agent-idle-minutes", type=float, default=120.0)
    parser.add_argument("--token-file", default=None,
                        help="read the bootstrap token from this 0600 file instead of the "
                             "environment, so it never lands in /proc/<pid>/environ")
    parser.add_argument("--out", default=None, help="call: save a dataUrl image to this file")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("command", nargs="?", choices=["status", "tools", "wait", "call"])
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args(argv)

    if args.version:
        print(f"stitchslop-bridge {PROTOCOL}")
        return 0

    if args.command:
        positional = [item for item in (args.rest or []) if not str(item).startswith("--")]
        args.seconds = positional[0] if args.command == "wait" and positional else None
        args.json_args = positional[0] if args.command == "call" and positional else None
        return cli(args)

    origins = [o.strip() for o in str(args.origin).split(",") if o.strip()]
    token = read_token(args.token_file)
    config_dir = Path(args.config_dir).expanduser()
    bridge = Bridge(origins, config_dir, args.port, args.agent_idle_minutes, token)

    if not token:
        # No bootstrap credential: we can only run if some already-allowed origin
        # has a stored pairing. Otherwise there is no way to authenticate anyone.
        paired = [o for o in origins if bridge.paired_for(o)]
        if not paired:
            log("no --token-file and no stored pairing for "
                + ", ".join(origins) + " — nothing could authenticate. "
                "Open ⌁ Agent in Stitch Slop, switch on Enable Agent Connections, "
                "click Copy, and start the bridge with that token.")
            return 1
        log(f"no token supplied; using the stored pairing for {', '.join(paired)}")

    def _shutdown(signum, _frame):  # noqa: ANN001
        bridge.stop_reason = f"signal {signum}"
        bridge.stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    try:
        bridge.bind()
    except SystemExit:
        return 2
    try:
        bridge.serve()
    finally:
        bridge.remove_session()
        if bridge.stop_reason:
            log(f"exiting: {bridge.stop_reason}; the pairing is kept, so starting again needs no token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
