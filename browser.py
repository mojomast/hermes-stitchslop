#!/usr/bin/env python3
"""Own the Stitch Slop browser: launch Chrome, stream it, and forward input.

Three jobs, all on loopback:

* launch a dedicated Chrome profile on a private X display (headless Chrome
  emits no screencast frames for this app, so we use Xvfb + a real window);
* speak CDP to that tab: ``Page.startScreencast`` out, ``Input.*`` back in;
* serve the frames and take input on 127.0.0.1:8791-8794 for the Hermes pane.

Nothing here talks to the network: Chrome fetches the site, this module only
carries pixels and events over loopback.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

SITE = "https://www.stitchslop.com/"
RELAY_PORTS = (8791, 8792, 8793, 8794)
EVENT_LIMIT = 256      # actions kept for the viewer; it polls 8x a second
DISPLAY = ":99"
WIDTH, HEIGHT = 1440, 900
QUALITY = 68
CAPTURE_INTERVAL = 0.12  # seconds between stills; ~5 fps, one source only
STATE_DIR = Path.home() / ".stitchslop" / "pane"
CHROME_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/snap/bin/chromium"),
)
ALLOWED_ORIGIN = re.compile(r"^(https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?|app://[^/]*|file://|null)$", re.I)


def log(msg: str) -> None:
    print(f"[stitchslop-browser] {msg}", file=sys.stderr, flush=True)


def state_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR.parent, 0o700)
    except OSError:
        pass
    return STATE_DIR / "browser.json"


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(data: dict) -> None:
    path = state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


# --------------------------------------------------------------------- websocket
class WS:
    """Just enough RFC 6455 for CDP: one text stream, no extensions."""

    def __init__(self, url: str, timeout: float = 10.0):
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("cdp closed during handshake")
            head += chunk
        if b"101" not in head.split(b"\r\n")[0]:
            first = head.split(b"\r\n")[0]
            raise ConnectionError(f"cdp refused: {first!r}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self._send_lock = threading.Lock()

    def send(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack("!H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", n)
        frame = head + mask + bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(frame)

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("cdp eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout: float = 30.0) -> dict:
        self.sock.settimeout(timeout)
        b1, b2 = self._take(2)
        opcode = b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._take(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._take(8))[0]
        payload = self._take(n) if n else b""
        if opcode == 0x9:  # ping
            return {"method": "__ping__", "payload": payload}
        return json.loads(payload.decode("utf-8")) if payload else {}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------------ chrome
def hermes_home() -> Path:
    """The Hermes data directory, without assuming a home-directory layout."""
    from hermes_constants import get_hermes_home   # always available in the plugin host
    return get_hermes_home()


def pane_input(event: dict, timeout: float = 5.0) -> dict:
    """Send one input event *through the relay* so the overlay animates it.

    Agent-side actions otherwise go straight to CDP and the relay never sees them:
    the viewer would stay silent while the agent clicked all over the app. Anything
    the agent does to the UI should come through here instead of ``WS.send``.
    """
    state = read_state()
    port, token = state.get("relayPort"), state.get("relayToken")
    if not port or not token:
        return {"ok": False, "error": "no_relay"}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/input", data=json.dumps(event).encode(),
        headers={"x-stitchslop-pane": str(token), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as fh:
            return json.loads(fh.read())
    except OSError as exc:
        return {"ok": False, "error": "relay_unreachable", "message": str(exc)}


def pane_click(x: float, y: float, timeout: float = 5.0) -> dict:
    """Click at a viewport coordinate, visible in the pane."""
    down = pane_input({"kind": "mouse", "action": "down", "x": x, "y": y}, timeout)
    pane_input({"kind": "mouse", "action": "up", "x": x, "y": y}, timeout)
    return down


def pane_type(text: str, timeout: float = 5.0) -> dict:
    """Type into the focused element, visibly."""
    return pane_input({"kind": "text", "text": text}, timeout)


def chrome_binary() -> Path | None:
    # Prefer the Chromium Hermes already ships (a known-good build for this host),
    # then the system browser, then platform installs.
    for root in (hermes_home() / "tools", Path("/opt"), Path("/usr/lib")):
        for candidate in sorted(root.glob("chromium-*/chrome-linux64/chrome"), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in CHROME_PATHS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def chrome_env() -> dict:
    """A deliberately minimal environment for Chrome.

    Passing the whole of ``os.environ`` would hand the browser every API key in the
    agent's process; Chrome needs very little.
    """
    tmp = STATE_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "DISPLAY": DISPLAY,
        "HOME": str(Path.home()),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "XDG_CACHE_HOME": str(STATE_DIR / "cache"),
    }


def ensure_display() -> None:
    """Start Xvfb on DISPLAY if nothing is serving it."""
    number = DISPLAY.lstrip(":")
    if Path(f"/tmp/.X11-unix/X{number}").exists():
        return
    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", f"0 {WIDTH + 60}x{HEIGHT + 60}x24", "-nolisten", "tcp"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{number}").exists():
            log(f"started Xvfb on {DISPLAY} (pid {xvfb.pid})")
            return
        time.sleep(0.1)
    raise RuntimeError("Xvfb did not come up")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cdp_page(port: int, timeout: float = 25.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as fh:
                targets = json.load(fh)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target
        except Exception:  # noqa: BLE001 - chrome is still starting
            pass
        time.sleep(0.3)
    return None


def launch(url: str = SITE, force: bool = False) -> dict:
    """Start Chrome (and Xvfb) if it is not already ours. Idempotent."""
    state = read_state()
    if not force and state.get("chromePid") and pid_alive(state["chromePid"]) and state.get("cdpPort"):
        return {**state, "already": True}

    binary = chrome_binary()
    if not binary:
        return {"ok": False, "error": "no_chrome", "message": "no Chrome/Chromium binary found"}

    ensure_display()
    profile = STATE_DIR / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()
    args = [
        str(binary),
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        # The tab is served from https and must reach loopback. Chrome gates that
        # behind the Local Network Access permission prompt, which cannot be
        # answered in a windowless X session — the socket then hangs unopened.
        "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,"
        "PrivateNetworkAccessRespectPreflightResults,Translate",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--hide-scrollbars", f"--window-size={WIDTH},{HEIGHT}", "--window-position=0,0",
        f"--app={url}",
    ]
    proc = subprocess.Popen(args, env=chrome_env(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    target = cdp_page(port)
    if not target:
        proc.terminate()
        return {"ok": False, "error": "chrome_failed",
                "message": f"Chrome started (pid {proc.pid}) but exposed no page target on {port}"}
    state.update({"ok": True, "chromePid": proc.pid, "cdpPort": port,
                  "wsUrl": target["webSocketDebuggerUrl"], "url": target.get("url") or url,
                  "width": WIDTH, "height": HEIGHT, "startedAt": time.time()})
    write_state(state)
    log(f"chrome pid {proc.pid}, cdp {port}, page {target.get('url')}")
    return {**state, "already": False}


def stop() -> dict:
    state = read_state()
    pid = state.get("chromePid")
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), 15)
        except OSError:
            pass
    relay = state.get("relayPid")
    if relay and pid_alive(relay):
        try:
            os.kill(int(relay), 15)
        except OSError:
            pass
    # Stopping Chrome must not forget a deliberate overlay choice; keep just
    # that flag, so the next start comes back the way the user left it.
    overlay = state.get("overlay")
    try:
        state_path().unlink()
    except OSError:
        pass
    if overlay is not None:
        write_state({"overlay": bool(overlay)})
    return {"ok": True, "stopped": bool(pid)}


# ------------------------------------------------------------------- overlay
def overlay_enabled() -> bool:
    """Fancy mode is on unless the user turned it off; stored with the browser."""
    return bool(read_state().get("overlay", True))


def _post_overlay(port: Any, token: str, enabled: bool) -> bool:
    """Tell a running relay to flip the viewer overlay. Never raises."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/overlay",
        data=json.dumps({"enabled": bool(enabled)}).encode(),
        headers={"Content-Type": "application/json", "x-stitchslop-pane": token},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2) as fh:
            return bool(json.load(fh).get("ok"))
    except Exception:  # noqa: BLE001 - a dead relay just means the flag is stored
        return False


def set_overlay(enabled: bool) -> dict:
    """Persist the overlay flag and apply it to the live pane if there is one."""
    state = read_state()
    state["overlay"] = bool(enabled)
    write_state(state)
    live = False
    if state.get("relayPid") and pid_alive(state["relayPid"]) and state.get("relayPort"):
        live = _post_overlay(state["relayPort"], state.get("relayToken") or "", bool(enabled))
    return {"ok": True, "overlay": bool(enabled), "live": live}


# ------------------------------------------------------------------------- relay
class Relay:
    """Latest JPEG + input forwarding, shared by the HTTP threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.frame_seq = 0
        self.ws: WS | None = None
        self.connected = False
        self.last_error = ""
        self.msg_id = 0
        self._id_lock = threading.Lock()
        # Fancy mode: a small ring buffer of the actions this relay forwarded.
        # It is fed in dispatch(), the one choke point for viewer posts and for
        # Python callers alike, and drained by GET /events?since=<id>.
        self.events: list[dict] = []
        self.event_id = 0
        self.overlay = True

    def next_id(self) -> int:
        with self._id_lock:
            self.msg_id += 1
            return self.msg_id

    # ------------------------------------------------------------ action events
    def record(self, kind: str, **fields: Any) -> dict:
        """Append one forwarded action; ids are monotonic for the viewer's cursor."""
        with self.lock:
            self.event_id += 1
            event: dict[str, Any] = {"id": self.event_id, "kind": kind,
                                     "ts": round(time.time(), 3)}
            event.update({k: v for k, v in fields.items() if v is not None})
            self.events.append(event)
            if len(self.events) > EVENT_LIMIT:
                del self.events[: len(self.events) - EVENT_LIMIT]
            return event

    def events_since(self, since: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["id"] > since]

    def _capture_loop(self, stop: threading.Event) -> None:
        """Steady stills, one after another, never overlapping a pending one."""
        while not stop.is_set():
            ws = self.ws
            if ws is None:
                stop.wait(0.5)
                continue
            with self.lock:
                before = self.frame_seq
            try:
                ws.send({"id": self.next_id(), "method": "Page.captureScreenshot",
                         "params": {"format": "jpeg", "quality": QUALITY}})
            except OSError:
                stop.wait(0.5)
                continue
            deadline = time.time() + 1.5
            while time.time() < deadline and not stop.is_set():
                with self.lock:
                    if self.frame_seq > before:
                        break
                time.sleep(0.02)
            stop.wait(CAPTURE_INTERVAL)

    # ------------------------------------------------------------- cdp thread
    def attach(self, ws_url: str) -> None:
        while True:
            try:
                self.ws = WS(ws_url, timeout=15)
                self.connected = True
                self.last_error = ""
                stop_capture = threading.Event()
                self.ws.send({"id": self.next_id(), "method": "Page.enable"})
                self.ws.send({"id": self.next_id(), "method": "Page.bringToFront"})
                self.ws.send({"id": self.next_id(), "method": "Emulation.setDeviceMetricsOverride",
                              "params": {"width": WIDTH, "height": HEIGHT,
                                         "deviceScaleFactor": 1, "mobile": False}})
                # One capture path only. Page.startScreencast and
                # Page.captureScreenshot come off different pipelines and grade
                # differently, so alternating between them made the pane flash
                # between two contrasts. A steady still loop costs a few ms per
                # frame at this size and always looks the same.
                threading.Thread(target=self._capture_loop, args=(stop_capture,),
                                 name="capture", daemon=True).start()
                log("capture loop started")
                try:
                    while True:
                        try:
                            msg = self.ws.recv(30)
                        except TimeoutError:
                            continue
                        data = (msg.get("result") or {}).get("data")
                        if data and not msg.get("method"):
                            with self.lock:
                                self.frame = base64.b64decode(data)
                                self.frame_seq += 1
                finally:
                    stop_capture.set()
            except Exception as exc:  # noqa: BLE001 - reconnect forever
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                log(f"cdp connection lost ({self.last_error}); retrying")
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
                time.sleep(1.0)

    def dispatch(self, event: dict) -> dict:
        ws = self.ws
        if not ws:
            return {"ok": False, "error": "not_connected"}
        kind = event.get("kind")
        try:
            if kind == "mouse":
                action = event.get("action")
                button = event.get("button") or "left"
                buttons = 1 if action == "down" and button == "left" else 0
                if action == "wheel":
                    ws.send({"id": self.next_id(), "method": "Input.dispatchMouseEvent",
                             "params": {"type": "mouseWheel", "x": event["x"], "y": event["y"],
                                        "deltaX": event.get("dx", 0), "deltaY": event.get("dy", 0)}})
                    self.record("wheel", x=event["x"], y=event["y"],
                                dx=event.get("dx", 0), dy=event.get("dy", 0))
                else:
                    ws.send({"id": self.next_id(), "method": "Input.dispatchMouseEvent",
                             "params": {"type": {"down": "mousePressed", "up": "mouseReleased",
                                                 "move": "mouseMoved"}[action],
                                        "x": event["x"], "y": event["y"], "button": button,
                                        "buttons": buttons, "clickCount": 1 if action != "move" else 0,
                                        "modifiers": int(event.get("mods") or 0)}})
                    # One blip per click, not one per down+up pair; drags are
                    # continuous and would swamp the viewer's queue.
                    if action == "down":
                        self.record("click", x=event["x"], y=event["y"], button=button)
            elif kind == "text":
                text = str(event.get("text") or "")
                if not text:
                    return {"ok": True}
                ws.send({"id": self.next_id(), "method": "Input.insertText",
                         "params": {"text": text}})
                self.record("text", text=text)
            elif kind == "key":
                key = str(event.get("key") or "")
                ws.send({"id": self.next_id(), "method": "Input.dispatchKeyEvent",
                         "params": {"type": "keyDown", "key": key,
                                    "code": event.get("code") or key,
                                    "windowsVirtualKeyCode": int(event.get("vk") or 0),
                                    "modifiers": int(event.get("mods") or 0)}})
                ws.send({"id": self.next_id(), "method": "Input.dispatchKeyEvent",
                         "params": {"type": "keyUp", "key": key,
                                    "code": event.get("code") or key,
                                    "windowsVirtualKeyCode": int(event.get("vk") or 0),
                                    "modifiers": int(event.get("mods") or 0)}})
                self.record("key", key=key, code=event.get("code") or key,
                            mods=int(event.get("mods") or 0))
            elif kind == "nav":
                url = str(event.get("url") or SITE)
                ws.send({"id": self.next_id(), "method": "Page.navigate",
                         "params": {"url": url}})
                self.record("nav", url=url)
            else:
                return {"ok": False, "error": "bad_event"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        return {"ok": True}


VIEWER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stitch Slop</title>
<style>
  html,body{margin:0;height:100%;background:#0b0b0c;color:#ddd;font:12px/1.4 ui-sans-serif,system-ui,sans-serif}
  #wrap{position:absolute;inset:0;display:flex;flex-direction:column}
  #bar{display:flex;gap:8px;align-items:center;padding:5px 8px;border-bottom:1px solid #242427;color:#9a9aa2}
  #dot{width:7px;height:7px;border-radius:50%;background:#8a8a8a}
  #screen{flex:1;min-height:0;position:relative;background:#000;outline:none;overflow:hidden}
  #screen img{position:absolute;inset:0;margin:auto;max-width:100%;max-height:100%;object-fit:contain;user-select:none;-webkit-user-drag:none}
  #a{opacity:1}
  #hint{padding:3px 8px;color:#77707a;border-top:1px solid #1d1d20}
  .foc{box-shadow:inset 0 0 0 1px #4a9eff}

  /* fancy mode: effects live above the video, never move it */
  #fx{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:2}
  .blip{position:absolute;width:0;height:0;pointer-events:none}
  .blip:before{content:"";position:absolute;left:-24px;top:-24px;width:48px;height:48px;border-radius:50%;
    background:radial-gradient(circle,rgba(3,6,11,.5),rgba(3,6,11,.22) 48%,rgba(3,6,11,0) 72%);
    animation:halo 620ms ease-out forwards}
  .blip i{position:absolute;left:0;top:0;border-radius:50%;display:block}
  .blip .r1{width:14px;height:14px;margin:-7px 0 0 -7px;border:2px solid rgba(150,242,255,.98);
    box-shadow:0 0 18px rgba(88,216,255,.8);animation:ringOut 1200ms cubic-bezier(.16,.68,.24,1) forwards}
  .blip .r2{width:20px;height:20px;margin:-10px 0 0 -10px;border:1px solid rgba(126,236,255,.5);
    animation:ringOut 1400ms 100ms cubic-bezier(.16,.68,.24,1) forwards}
  .blip .core{width:10px;height:10px;margin:-5px 0 0 -5px;
    background:radial-gradient(circle at 34% 32%,#fff 0 22%,#9beeff 46%,#3aa7ff 78%);
    box-shadow:0 0 0 1px rgba(2,14,22,.55),0 0 12px rgba(140,238,255,.95),0 0 28px rgba(48,150,255,.65);
    animation:corePop 1000ms ease-out forwards}
  @keyframes halo{0%{opacity:0;transform:scale(.55)}18%{opacity:1}100%{opacity:0;transform:scale(1.25)}}
  @keyframes ringOut{0%{transform:scale(.22);opacity:0}8%{opacity:1}55%{opacity:.64}100%{transform:scale(3.4);opacity:0}}
  @keyframes corePop{0%{transform:scale(.35);opacity:0}14%{transform:scale(1.25);opacity:1}55%{opacity:.92}100%{transform:scale(.82);opacity:0}}

  .bubble{position:absolute;max-width:300px;padding:8px 12px 8px;border-radius:12px;
    background:rgba(15,17,22,.9);border:1px solid rgba(126,236,255,.3);
    box-shadow:0 10px 30px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.05),0 0 24px rgba(60,170,255,.12);
    color:#eaf7ff;font-size:12.5px;line-height:1.45;letter-spacing:.01em;white-space:pre-wrap;word-break:break-word;
    transform:translate(-50%,calc(-100% - 16px));transform-origin:50% 100%;pointer-events:none;
    animation:bubbleIn 160ms cubic-bezier(.2,.8,.2,1) backwards}
  .bubble.below{transform:translate(-50%,16px);transform-origin:50% 0}
  .bubble.done{border-color:rgba(126,236,255,.46)}
  .bubble.out{opacity:0;transform:translate(-50%,calc(-100% - 24px));transition:opacity 260ms ease,transform 260ms ease}
  .bubble.below.out{transform:translate(-50%,22px)}
  .bubble .tx{white-space:pre-wrap}
  .bubble .caret{display:inline-block;width:2px;height:1.05em;margin:0 0 -2px 2px;border-radius:1px;
    background:#8ceaff;box-shadow:0 0 8px rgba(120,232,255,.9);animation:caret 720ms steps(1) infinite;vertical-align:baseline}
  .bubble.done .caret{animation:none;opacity:0}
  @keyframes caret{0%,49%{opacity:1}50%,100%{opacity:.15}}
  @keyframes bubbleIn{from{opacity:0;transform:translate(-50%,calc(-100% - 6px)) scale(.96)}to{opacity:1}}

  .key{position:absolute;left:50%;bottom:18px;transform:translateX(-50%);display:flex;align-items:center;gap:6px;
    padding:4px 5px;border-radius:12px;background:rgba(10,11,14,.62);backdrop-filter:blur(2px);
    box-shadow:0 8px 24px rgba(0,0,0,.5);pointer-events:none;
    animation:keyIn 180ms cubic-bezier(.2,.9,.3,1.2) backwards}
  .key .cap{min-width:28px;padding:5px 10px 6px;border-radius:8px;text-align:center;
    background:linear-gradient(#3b404b,#24272e);border:1px solid #565c69;border-bottom:2px solid #16181c;
    box-shadow:0 3px 0 #0b0c0e,0 8px 20px rgba(0,0,0,.55),inset 0 1px 0 rgba(255,255,255,.12);
    color:#f2f6fc;font:600 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.03em;white-space:nowrap}
  .key .mul{color:#9a9aa2;font:600 10px/1 ui-sans-serif,system-ui,sans-serif}
  .key.out{opacity:0;transform:translate(-50%,6px);transition:opacity 240ms ease,transform 240ms ease}
  @keyframes keyIn{from{opacity:0;transform:translate(-50%,8px) scale(.9)}to{opacity:1;transform:translateX(-50%) scale(1)}}

  .nav{position:absolute;left:50%;top:12px;transform:translateX(-50%);padding:5px 11px;border-radius:999px;
    background:rgba(15,17,22,.88);border:1px solid rgba(126,236,255,.25);color:#cfefff;
    font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;pointer-events:none;
    box-shadow:0 8px 22px rgba(0,0,0,.45);animation:navIn 260ms ease both}
  .nav:after{content:"";position:absolute;left:8px;right:8px;bottom:-1px;height:1px;
    background:linear-gradient(90deg,transparent,#7eeaff,transparent);animation:sweep 1.05s ease-in-out infinite}
  @keyframes navIn{from{opacity:0;transform:translate(-50%,-6px)}to{opacity:1}}
  @keyframes sweep{0%,100%{transform:scaleX(.15);opacity:.25}50%{transform:scaleX(1);opacity:.9}}

  #fancy{position:absolute;right:10px;bottom:10px;z-index:4;display:flex;align-items:center;gap:6px;
    padding:5px 10px 6px;border-radius:999px;cursor:pointer;
    background:rgba(16,17,21,.78);border:1px solid #2c2e34;color:#7d7d86;
    font:600 10.5px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:.04em;
    opacity:.72;transition:opacity 160ms ease,border-color 160ms ease,color 160ms ease,box-shadow 160ms ease}
  #fancy:hover{opacity:1}
  #fancy .star{font-size:12px;line-height:1;color:#5c5e66;transition:color 160ms ease,text-shadow 160ms ease}
  #fancy.on{border-color:rgba(126,236,255,.5);color:#cfeeff;box-shadow:0 0 18px rgba(60,170,255,.18),inset 0 0 12px rgba(60,170,255,.06)}
  #fancy.on .star{color:#8ceaff;text-shadow:0 0 10px rgba(120,232,255,.9)}
  #q{position:absolute;right:10px;bottom:42px;z-index:4;padding:3px 8px;border-radius:999px;
    background:rgba(16,17,21,.82);border:1px solid rgba(126,236,255,.22);color:#9fdfee;
    font:600 9.5px/1.4 ui-sans-serif,system-ui,sans-serif;letter-spacing:.03em;pointer-events:none}
</style></head><body><div id="wrap">
<div id="bar"><span id="dot"></span><b>Stitch Slop</b><span id="state">connecting…</span>
<span style="flex:1"></span><a href="#" id="reload" style="color:#9a9aa2">reload site</a></div>
<div id="screen" tabindex="0"><img id="a" alt="">
<div id="fx"></div>
<div id="q" hidden></div>
<button id="fancy" class="on" aria-pressed="true" title="Show agent actions in the preview"><span class="star">✦</span><span>actions</span></button>
</div>
<div id="hint">click to focus — keys and clicks go to the app</div>
</div>
<script>
const TOKEN = "__TOKEN__";
const BUILD = "__BUILD__";
const W = __W__, H = __H__;
const screen = document.getElementById('screen');
const img = document.getElementById('a');
const stateEl = document.getElementById('state'), dot = document.getElementById('dot');
const fx = document.getElementById('fx'), fancyBtn = document.getElementById('fancy'), qEl = document.getElementById('q');
let seq = 0;

function show(_ignored, url) {
  // One <img>, swapped only after the next frame has decoded: no cross-fade,
  // so nothing can blend two differently graded frames together.
  const probe = new Image();
  probe.onload = () => {
    img.src = url;
    dot.style.background = '#2fbf71';
    stateEl.textContent = 'live';
  };
  probe.onerror = () => { dot.style.background = '#d9a441'; };
  probe.src = url;
}

let relayFancy = null;
async function tick() {
  try {
    const r = await fetch('/ping', {headers:{'x-stitchslop-pane': TOKEN}});
    const j = await r.json();
    if (j.build && j.build !== BUILD) { location.reload(); return; }
    if (!j.connected) { stateEl.textContent = 'chrome connecting…'; dot.style.background = '#d9a441'; }
    // Edge-triggered: a local toggle stays put until the relay flag changes.
    if (typeof j.overlay === 'boolean' && j.overlay !== relayFancy) {
      relayFancy = j.overlay;
      setFancy(j.overlay);
    }
    show(null, '/frame.jpg?seq=' + (seq++));
  } catch (e) { stateEl.textContent = 'relay lost'; dot.style.background = '#c0392b'; }
  setTimeout(tick, 130);
}

/* ---------------------------------------------------------------- fancy mode
   Actions arrive from GET /events?since=<id> at 8/s. They are queued and
   played one at a time so a burst stays readable; the input already reached
   Chrome the moment it was posted, only this animation is paced. */
const PAGE_T0 = Date.now();
const BEAT = 320;          // ms between click/key blips
const TYPE_MS = 28;        // ~36 chars/s
let fancy = true;
let queue = [];
let lastId = 0;
let draining = false;
let lastPoint = {x: W / 2, y: H / 2};
let keyNode = null;
const stats = {received: 0, played: 0};

function setFancy(on) {
  fancy = !!on;
  fancyBtn.classList.toggle('on', fancy);
  fancyBtn.setAttribute('aria-pressed', fancy ? 'true' : 'false');
  if (!fancy) {
    queue = [];
    fx.textContent = '';
    updateQ();
  }
}

function updateQ() {
  const n = queue.length;
  qEl.hidden = n < 2;
  if (n > 1) qEl.textContent = n + ' queued';
}

function toScreen(x, y) {
  const r = screen.getBoundingClientRect();
  const s = Math.min(r.width / W, r.height / H);
  const ox = (r.width - W * s) / 2, oy = (r.height - H * s) / 2;
  return {x: ox + Number(x) * s, y: oy + Number(y) * s};
}

function blip(x, y) {
  const p = toScreen(x, y);
  const el = document.createElement('div');
  el.className = 'blip';
  el.style.left = p.x + 'px';
  el.style.top = p.y + 'px';
  for (const cls of ['r1', 'r2', 'core']) {
    const i = document.createElement('i');
    i.className = cls;
    el.appendChild(i);
  }
  fx.appendChild(el);
  lastPoint = p;
  setTimeout(() => el.remove(), 1250);
}

const KEY_LABEL = {Backspace: '⌫', Enter: '⏎', Tab: '⇥', Escape: 'esc', Delete: '⌦',
                   ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→', ' ': 'space'};
function keyLabel(ev) {
  const raw = String(ev.key || '');
  let label = KEY_LABEL[raw] || raw;
  if (label.length > 10) label = label.slice(0, 9) + '…';
  let pre = '';
  const mods = Number(ev.mods || 0);
  if (mods & 2) pre += 'Ctrl+';
  if (mods & 4) pre += '⌘';
  if (mods & 1) pre += '⌥';
  if (mods & 8) pre += '⇧';
  return pre + label;
}

function keycap(ev) {
  if (keyNode) keyNode.remove();   // one key slot: a new key replaces the last
  const el = document.createElement('div');
  el.className = 'key';
  const cap = document.createElement('span');
  cap.className = 'cap';
  cap.textContent = keyLabel(ev);
  el.appendChild(cap);
  if (Number(ev.count) > 1) {
    const mul = document.createElement('span');
    mul.className = 'mul';
    mul.textContent = '×' + Number(ev.count);
    el.appendChild(mul);
  }
  fx.appendChild(el);
  keyNode = el;
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => {
      el.remove();
      if (keyNode === el) keyNode = null;
    }, 260);
  }, 1100);
}

function bubble(text) {
  const el = document.createElement('div');
  el.className = 'bubble';
  const tx = document.createElement('span');
  tx.className = 'tx';
  const caret = document.createElement('span');
  caret.className = 'caret';
  el.appendChild(tx);
  el.appendChild(caret);
  el.style.left = lastPoint.x + 'px';
  el.style.top = lastPoint.y + 'px';
  fx.appendChild(el);

  const shown = text.length > 72 ? text.slice(0, 71) + '…' : text;
  const sr = screen.getBoundingClientRect();
  const br = el.getBoundingClientRect();
  let left = Math.max(br.width / 2 + 8, Math.min(sr.width - br.width / 2 - 8, lastPoint.x));
  let below = lastPoint.y - br.height - 16 < 8;
  el.classList.toggle('below', below);
  el.style.left = left + 'px';
  if (!below) el.style.top = Math.max(br.height + 16, lastPoint.y) + 'px';

  let i = 0;
  const timer = setInterval(() => {
    tx.textContent = shown.slice(0, ++i);
    if (i >= shown.length) {
      clearInterval(timer);
      el.classList.add('done');
      setTimeout(() => {
        el.classList.add('out');
        setTimeout(() => el.remove(), 300);
      }, 500);
    }
  }, TYPE_MS);
  if (!shown.length) {
    clearInterval(timer);
    el.classList.add('done');
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, 500);
  }
  return Math.max(1, shown.length) * TYPE_MS + 820;
}

function navFlash() {
  const el = document.createElement('div');
  el.className = 'nav';
  el.textContent = 'reloading';
  fx.appendChild(el);
  setTimeout(() => el.remove(), 1400);
}

function play(ev) {
  if (ev.kind === 'click') {
    blip(ev.x, ev.y);
    return BEAT;
  }
  if (ev.kind === 'text') return bubble(String(ev.text || ''));
  if (ev.kind === 'key') {
    keycap(ev);
    return BEAT;
  }
  if (ev.kind === 'wheel') {
    blip(ev.x, ev.y);
    return 260;
  }
  if (ev.kind === 'nav') {
    navFlash();
    return 700;
  }
  return 220;
}

function drain() {
  if (draining || !fancy) return;
  draining = true;
  const step = () => {
    if (!fancy) { draining = false; queue = []; updateQ(); return; }
    const ev = queue.shift();
    updateQ();
    if (!ev) { draining = false; return; }
    stats.played++;
    setTimeout(step, play(ev));
  };
  step();
}

function consider(ev) {
  if (!ev || typeof ev.id !== 'number' || ev.id <= lastId) return;
  lastId = ev.id;
  if (!fancy) return;
  // A reload must not replay the relay's whole ring buffer as a stale burst.
  if (ev.ts && ev.ts * 1000 < PAGE_T0 - 400) return;
  stats.received++;
  const tail = queue[queue.length - 1];
  if (ev.kind === 'text' && tail && tail.kind === 'text') {  // per-character inserts
    tail.text += String(ev.text || '');
    return;
  }
  if (ev.kind === 'key' && tail && tail.kind === 'key' && tail.key === ev.key && tail.mods === ev.mods) {
    tail.count = (tail.count || 1) + 1;                      // key repeat
    return;
  }
  if (ev.kind === 'key') ev.count = 1;
  queue.push(ev);
  updateQ();
  drain();
}

async function evTick() {
  try {
    const r = await fetch('/events?since=' + lastId, {headers:{'x-stitchslop-pane': TOKEN}});
    if (r.ok) {
      const list = await r.json();
      if (Array.isArray(list)) for (const ev of list) consider(ev);
    }
  } catch (e) { /* the frame poll reports a dead relay */ }
  setTimeout(evTick, 125);
}

fancyBtn.addEventListener('pointerdown', e => { e.stopPropagation(); e.preventDefault(); setFancy(!fancy); });
fancyBtn.addEventListener('pointerup', e => e.stopPropagation());
fancyBtn.addEventListener('click', e => e.stopPropagation());
fancyBtn.addEventListener('keydown', e => e.stopPropagation());

window.__stitchslopFancy = () => ({enabled: fancy, queued: queue.length,
  received: stats.received, played: stats.played, nodes: fx.children.length});

tick();
evTick();

function post(ev) {
  fetch('/input', {method:'POST', headers:{'Content-Type':'application/json','x-stitchslop-pane':TOKEN}, body: JSON.stringify(ev)}).catch(()=>{});
}
function scale(e) {
  const r = screen.getBoundingClientRect();
  const s = Math.min(r.width / W, r.height / H);
  const ox = (r.width - W * s) / 2, oy = (r.height - H * s) / 2;
  return { x: Math.round((e.clientX - r.left - ox) / s), y: Math.round((e.clientY - r.top - oy) / s) };
}
function mods(e){ return (e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0); }
screen.addEventListener('pointerdown', e => { screen.focus(); screen.classList.add('foc'); const p = scale(e); post({kind:'mouse',action:'down',x:p.x,y:p.y,button:'left',mods:mods(e)}); });
screen.addEventListener('pointermove', e => { if (e.buttons === 0) return; const p = scale(e); post({kind:'mouse',action:'move',x:p.x,y:p.y,button:'left',mods:mods(e)}); });
screen.addEventListener('pointerup', e => { const p = scale(e); post({kind:'mouse',action:'up',x:p.x,y:p.y,button:'left',mods:mods(e)}); });
screen.addEventListener('wheel', e => { e.preventDefault(); const p = scale(e); post({kind:'mouse',action:'wheel',x:p.x,y:p.y,dx:e.deltaX,dy:e.deltaY}); }, {passive:false});
screen.addEventListener('keydown', e => {
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); post({kind:'text',text:e.key}); return; }
  const vk = {Enter:13,Backspace:8,Tab:9,Escape:27,Delete:46,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40,' ':32}[e.key];
  if (vk !== undefined) { e.preventDefault(); post({kind:'key',key:e.key,code:e.code,vk,mods:mods(e)}); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.length === 1) { e.preventDefault(); post({kind:'key',key:e.key,code:e.code,vk:e.key.toUpperCase().charCodeAt(0),mods:mods(e)}); }
});
document.getElementById('reload').addEventListener('click', e => { e.preventDefault(); post({kind:'nav',url:'https://www.stitchslop.com/'}); });
</script></body></html>
"""


def make_handler(relay: Relay, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stitchslop-pane/1"

        def log_message(self, *_args):  # keep the log quiet; the agent has its own
            pass

        def _note(self) -> None:
            origin = self.headers.get("Origin") or "(none)"
            key = (self.command, self.path.split("?")[0], origin)
            with relay.lock:
                seen = getattr(relay, "_seen", None)
                if seen is None:
                    seen = relay._seen = set()
                if key in seen:
                    return
                seen.add(key)
            log(f"pane request {key[0]} {key[1]} origin={key[2]}")

        def _cors(self) -> bool:
            origin = self.headers.get("Origin") or ""
            if origin and ALLOWED_ORIGIN.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                return True
            return False

        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._note()
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, x-stitchslop-pane")
            # Private Network Access: a file:// renderer reaching 127.0.0.1 is a
            # private-network request; without this grant Chromium drops it before
            # it ever leaves the page (no request, no error we can log server-side).
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._note()
            route = self.path.split("?")[0]
            if route == "/ping":
                with relay.lock:
                    seq = relay.frame_seq
                payload = {"ok": True, "connected": relay.connected, "frames": seq,
                           "width": WIDTH, "height": HEIGHT, "error": relay.last_error,
                           "overlay": relay.overlay, "build": token[:8]}
                # The pane is a loopback-origin document; a remote page gets no token,
                # and without it /input refuses the request.
                origin = self.headers.get("Origin") or ""
                if origin and ALLOWED_ORIGIN.match(origin):
                    payload["token"] = token
                self._json(200, payload)
                return
            if route in ("/viewer", "/"):
                html = (VIEWER_HTML.replace("__TOKEN__", token).replace("__BUILD__", token[:8])
                        .replace("__W__", str(WIDTH)).replace("__H__", str(HEIGHT)))
                data = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if route == "/events":
                # Typed text can pass through here, so it takes the same token
                # as /input. The viewer is same-origin; the token is the boundary.
                if self.headers.get("x-stitchslop-pane") != token:
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return
                query = parse_qs(self.path.partition("?")[2])
                try:
                    since = int((query.get("since") or ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                self._json(200, relay.events_since(since))
                return
            if route == "/frame.jpg":
                with relay.lock:
                    data = relay.frame
                    seq = relay.frame_seq
                if not data:
                    self._json(503, {"ok": False, "error": "no_frame"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("x-frame-seq", str(seq))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
                return
            self._json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            self._note()
            route = self.path.split("?")[0]
            if route not in ("/input", "/overlay"):
                self._json(404, {"ok": False, "error": "not_found"})
                return
            origin = self.headers.get("Origin") or ""
            if origin and not ALLOWED_ORIGIN.match(origin):
                self._json(403, {"ok": False, "error": "bad_origin"})
                return
            if self.headers.get("x-stitchslop-pane") != token:
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                event = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"ok": False, "error": "bad_json"})
                return
            if not isinstance(event, dict):
                self._json(400, {"ok": False, "error": "bad_json"})
                return
            if route == "/overlay":
                relay.overlay = bool(event.get("enabled"))
                self._json(200, {"ok": True, "overlay": relay.overlay})
                return
            self._json(200, relay.dispatch(event))

    return Handler


def bind_hosts() -> list[str]:
    """Viewer listen addresses.

    Default is loopback only. Set ``STITCHSLOP_PANE_BIND`` to a comma-separated
    host list (for example a Tailscale IP, or the literal ``tailscale`` to
    discover one) when a remote browser must reach the viewer. The LAN
    interface is never auto-bound.
    """
    hosts = ["127.0.0.1"]
    override = (os.environ.get("STITCHSLOP_PANE_BIND") or "").strip()
    if not override:
        return hosts
    parts = [h.strip() for h in override.split(",") if h.strip()]
    if parts == ["loopback"] or parts == ["127.0.0.1"]:
        return ["127.0.0.1"]
    resolved: list[str] = []
    for part in parts:
        if part in {"tailscale", "tailnet"}:
            try:
                out = subprocess.run(
                    ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3,
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                )
                for line in out.stdout.splitlines():
                    ip = line.strip()
                    if ip and ip not in resolved:
                        resolved.append(ip)
            except (OSError, subprocess.SubprocessError):
                pass
            if not resolved:
                try:
                    out = subprocess.run(
                        ["ip", "-4", "-o", "addr", "show", "tailscale0"],
                        capture_output=True, text=True, timeout=3,
                        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "C.UTF-8"},
                    )
                    for token in out.stdout.split():
                        if "/" in token and token.split("/")[0].count(".") == 3:
                            ip = token.split("/")[0]
                            if ip not in resolved:
                                resolved.append(ip)
                except (OSError, subprocess.SubprocessError):
                    pass
            continue
        if part not in resolved:
            resolved.append(part)
    return resolved or hosts


def public_host() -> str:
    hosts = bind_hosts()
    return hosts[-1] if len(hosts) > 1 else "127.0.0.1"


def serve(port: int, relay: Relay, token: str, ready: threading.Event) -> list[ThreadingHTTPServer]:
    servers = []
    for host in bind_hosts():
        try:
            httpd = ThreadingHTTPServer((host, port), make_handler(relay, token))
        except OSError:
            for started in servers:
                started.server_close()
            raise
        httpd.daemon_threads = True
        servers.append(httpd)
    for httpd in servers:
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    ready.set()
    return servers


def start_foreground(url: str = SITE) -> int:
    """Run the relay in this process (the plugin spawns this as a subprocess)."""
    info = launch(url)
    if not info.get("ok"):
        print(json.dumps(info), flush=True)
        return 1
    relay = Relay()
    relay.overlay = overlay_enabled()
    threading.Thread(target=relay.attach, args=(info["wsUrl"],), daemon=True).start()
    token = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    last_error = None
    for port in RELAY_PORTS:
        ready = threading.Event()
        try:
            serve(port, relay, token, ready)
        except OSError as exc:
            last_error = exc
            continue
        break
    else:
        print(json.dumps({"ok": False, "error": "no_relay_port", "message": str(last_error)}), flush=True)
        return 1
    state = read_state()
    state.update({"relayPid": os.getpid(), "relayPort": port, "relayToken": token})
    write_state(state)
    print(json.dumps({"ok": True, "port": port, "token": token, "url": url}), flush=True)
    log(f"relay on 127.0.0.1:{port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def ensure_relay(url: str = SITE, overlay: bool | None = None) -> dict:
    """Make sure Chrome + relay are up; start the relay subprocess if needed.

    ``overlay`` persists the fancy-mode flag and applies it to a relay that is
    already running; ``None`` leaves the stored value alone.
    """
    state = read_state()
    if overlay is not None:
        state["overlay"] = bool(overlay)
        write_state(state)
    if state.get("relayPid") and pid_alive(state["relayPid"]) and state.get("relayPort"):
        with urllib.request.urlopen(f"http://127.0.0.1:{state['relayPort']}/ping", timeout=3) as fh:
            ping = json.load(fh)
        if overlay is not None:
            _post_overlay(state["relayPort"], state.get("relayToken") or "", bool(overlay))
        return {"ok": True, "port": state["relayPort"], "token": state.get("relayToken"),
                "viewerUrl": f"http://{public_host()}:{state['relayPort']}/viewer?t={state.get('relayToken')}",
                "already": True, "connected": ping.get("connected"), "frames": ping.get("frames"),
                "overlay": bool(state.get("overlay", True))}

    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", url],
                            env=chrome_env(),
                            stdout=subprocess.PIPE, stderr=open(STATE_DIR / "relay.log", "ab"),
                            start_new_session=True)
    line = proc.stdout.readline().decode() if proc.stdout else ""
    try:
        info = json.loads(line)
    except ValueError:
        return {"ok": False, "error": "relay_failed", "message": line[:300] or "relay printed nothing"}
    if not info.get("ok"):
        return info
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/ping", timeout=2) as fh:
                ping = json.load(fh)
            if ping.get("frames", 0) > 0:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    if overlay is not None:
        _post_overlay(info["port"], info["token"], bool(overlay))
    return {"ok": True, "port": info["port"], "token": info["token"],
            "viewerUrl": f"http://{public_host()}:{info['port']}/viewer?t={info['token']}",
            "already": False, "connected": ping.get("connected"), "frames": ping.get("frames"),
            "overlay": overlay_enabled()}


def status() -> dict:
    state = read_state()
    chrome_ok = bool(state.get("chromePid") and pid_alive(state["chromePid"]))
    relay_ok = bool(state.get("relayPid") and pid_alive(state["relayPid"]))
    out = {"ok": True, "chrome": chrome_ok, "relay": relay_ok,
           "cdpPort": state.get("cdpPort"), "relayPort": state.get("relayPort"),
           "width": WIDTH, "height": HEIGHT, "overlay": bool(state.get("overlay", True))}
    if relay_ok:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{state['relayPort']}/ping", timeout=2) as fh:
                out["pane"] = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            out["pane"] = {"ok": False, "error": str(exc)}
    return out


if __name__ == "__main__":
    if "--serve" in sys.argv:
        target = sys.argv[sys.argv.index("--serve") + 1] if len(sys.argv) > sys.argv.index("--serve") + 1 else SITE
        raise SystemExit(start_foreground(target))
    print(json.dumps(status(), indent=1))
