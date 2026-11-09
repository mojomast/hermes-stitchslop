"""Stitch Slop for Hermes: drive the embroidery app in its own browser tab.

The app lives in a browser tab; this plugin owns the bridge that tab dials into
(loopback WebSocket, protocol 1 of https://www.stitchslop.com/agent) and the
Chrome window the pane streams. Tools are thin wrappers over the bridge's HTTP
control plane, so every command the app grows is reachable without a plugin
release.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
TOOLSET = "stitchslop"
CONFIG_DIR = Path.home() / ".stitchslop"
ASSETS = CONFIG_DIR / "pane"
EXPORTS = CONFIG_DIR / "exports"

STATUS_SCHEMA = {
    "name": "stitchslop_status",
    "description": "Stitch Slop state: whether the bridge is running, whether the user's tab is attached, the command count, and whether the pane's Chrome window is up.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

CONNECT_SCHEMA = {
    "name": "stitchslop_connect",
    "description": (
        "Start the loopback bridge (if needed) and wait for the user's Stitch Slop tab "
        "to attach, using the stored pairing. First pairing is pane/hand only: this tool "
        "refuses a model-supplied token (returns unsupported_operation / pair_via_pane). "
        "Pair once by hand with --token-file, then call stitchslop_connect() with no token."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "token": {
                "type": "string",
                "description": (
                    "Unsupported. First pairing is pane/hand only; a model-supplied token "
                    "returns unsupported_operation. Leave unset; reconnect with the stored pairing."
                ),
            },
            "origin": {"type": "string", "description": "Page origin allowed to connect; defaults to www + beta stitchslop.com."},
            "wait_seconds": {"type": "integer", "description": "How long to wait for the tab, default 60."},
        },
        "additionalProperties": False,
    },
}

CALL_SCHEMA = {
    "name": "stitchslop_call",
    "description": "Run one command on the attached Stitch Slop tab (the app owns the command list — read its descriptions first). Returns the app's JSON envelope; images are saved to disk and their path returned.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "e.g. scene.describe, scene.render, object.describe, select, transform, stitching.set."},
            "args": {"type": "object", "description": "Arguments for that command, matching its schema."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

BROWSER_SCHEMA = {
    "name": "stitchslop_browser",
    "description": "Control the pane's own Chrome window for Stitch Slop: start (launches Xvfb + Chrome + the screencast relay), stop, or status. Overlays click and typing animations in the pane.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status"]},
            "overlay": {"type": "boolean", "default": True,
                        "description": "Animate clicks, keys and typing in the pane (default true, persists)."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bridge_mod():
    return _load("stitchslop_bridge_mod", "bridge/stitchslop_bridge.py")


def browser_mod():
    return _load("stitchslop_browser_mod", "browser.py")


def _ok(payload: dict) -> str:
    return json.dumps(payload)


def _bridge_session() -> dict | None:
    bridge = bridge_mod()
    try:
        return bridge.pick_session(CONFIG_DIR, None)
    except SystemExit:
        return None


def _bridge_http(session: dict, method: str, route: str, payload: dict | None = None,
                 timeout: float = 120.0) -> dict:
    bridge = bridge_mod()
    _status, body = bridge.control(session, method, route, payload, timeout=timeout)
    return body


def _child_env() -> dict:
    """A minimal environment for the bridge process.

    Not ``os.environ``: the bridge needs no API keys, and a credential in a child's
    environment is readable from ``/proc/<pid>/environ``.
    """
    tmp = CONFIG_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
            "TMPDIR": str(tmp), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}


def _write_token_file(token: str) -> Path:
    """Hand the bootstrap token to the bridge as a 0600 file, not an env var."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.chmod(path, 0o600)
    return path


def start_bridge(token: str | None, origin: str | None, wait_seconds: int = 0) -> dict:
    session = _bridge_session()
    token_path = None
    if session is None:
        args = [sys.executable, str(_HERE / "bridge" / "stitchslop_bridge.py"),
                "--config-dir", str(CONFIG_DIR)]
        if token:
            token_path = _write_token_file(token)
            args += ["--token-file", str(token_path)]
        if origin:
            args += ["--origin", origin]
        log_path = ASSETS / "bridge.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log:
            subprocess.Popen(args, env=_child_env(), stdout=log, stderr=log,
                             start_new_session=True)
        deadline = time.time() + 6
        while time.time() < deadline and session is None:
            time.sleep(0.2)
            session = _bridge_session()
        if session is None:
            if token_path is not None:
                token_path.unlink(missing_ok=True)   # never leave a live token on disk
            return {"ok": False, "error": "bridge_failed",
                    "message": "the bridge did not start; see ~/.stitchslop/pane/bridge.log"}
    if token_path is not None:
        token_path.unlink(missing_ok=True)   # the bridge reads it once, at startup
    if wait_seconds:
        return _bridge_http(session, "POST", "/wait", {"seconds": wait_seconds},
                            timeout=wait_seconds + 15)
    return _bridge_http(session, "GET", "/status")


def _status(args: dict | None = None, **_kw) -> str:
    session = _bridge_session()
    out: dict = {"bridge": bool(session)}
    if session:
        try:
            out["tab"] = _bridge_http(session, "GET", "/status", timeout=8)
        except Exception as exc:  # noqa: BLE001
            out["tab"] = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    try:
        out["browser"] = browser_mod().status()
    except Exception as exc:  # noqa: BLE001
        out["browser"] = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    return _ok(out)


def _connect(args: dict | None = None, **_kw) -> str:
    """Reconnect with the stored pairing. Model-supplied tokens are refused.

    First pairing must stay a human action (Stitch Slop Agent panel + hand-started
    bridge with ``--token-file``), matching the Hermagotchi pane-only pairing rule:
    lobby/DM/page content must not be able to talk the agent into minting a pairing.
    """
    a = args or {}
    token = str(a.get("token") or "").strip() or None
    origin = str(a.get("origin") or "").strip() or None
    wait = int(a.get("wait_seconds") or 60)
    if token:
        # Keep the field for clear errors; never start a bridge with model input.
        return _ok({
            "ok": False,
            "error": "unsupported_operation",
            "code": "pair_via_pane",
            "message": (
                "First pairing is pane/hand only. stitchslop_connect refuses a "
                "model-supplied token so chat or page content cannot pair a stranger."
            ),
            "fix": (
                "In Stitch Slop open ⌁ Agent, enable connections, copy the tok_… line, "
                "write it to ~/.stitchslop/token at mode 0600, start the bridge once with "
                "--token-file ~/.stitchslop/token (then delete the file), and call "
                "stitchslop_connect() with no token to wait for the tab. Later sessions "
                "only need stitchslop_connect()."
            ),
        })
    result = start_bridge(None, origin, wait_seconds=wait)
    result.pop("pairingSecret", None)
    if result.get("error") == "bad_credential":
        result["fix"] = (
            "Stored pairing was rejected. Re-pair by hand with --token-file from a fresh "
            "Agent panel tok_…, then call stitchslop_connect() with no token."
        )
    return _ok(result)


def _write_data_url(data_url: str, path: Path) -> int:
    header, _, b64 = str(data_url).partition(",")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(b64)
    path.write_bytes(raw)
    return len(raw)


def _persist(reply: dict) -> None:
    """Move every base64 payload in an envelope onto disk.

    `scene.render` answers with one `dataUrl`; `design.export` answers with a
    `files` list whose entries each carry their own `dataUrl` (plus a shared
    `.col` sidecar). Dropping the list was silent data loss, so both shapes are
    handled here and the envelopes lose the base64 once it is written.
    """
    top = reply.pop("dataUrl", None)
    if top:
        header = str(top).partition(",")[0]
        suffix = ".png" if "png" in header else ".jpg" if ("jpeg" in header or "jpg" in header) else ".bin"
        path = ASSETS / f"last-render{suffix}"
        try:
            size = _write_data_url(top, path)
            reply["savedTo"] = str(path)
            reply["savedBytes"] = size
        except Exception as exc:  # noqa: BLE001
            reply["imageSaveError"] = f"{type(exc).__name__}: {exc}"
    files = reply.get("files")
    if isinstance(files, list) and files:
        saved: list[str] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            payload = entry.pop("dataUrl", None)
            if not payload:
                continue
            name = Path(str(entry.get("name") or "export.bin")).name
            path = EXPORTS / name
            try:
                size = _write_data_url(payload, path)
                entry["savedTo"] = str(path)
                entry["savedBytes"] = size
                saved.append(str(path))
            except Exception as exc:  # noqa: BLE001
                entry["saveError"] = f"{type(exc).__name__}: {exc}"
        if saved:
            reply["savedFiles"] = saved


def _call(args: dict | None = None, **_kw) -> str:
    a = args or {}
    command = str(a.get("command") or "").strip()
    if not command:
        return _ok({"ok": False, "error": "no_command", "fix": "pass command, e.g. scene.describe"})
    params = a.get("args") if isinstance(a.get("args"), dict) else {}
    session = _bridge_session()
    if session is None:
        return _ok({"ok": False, "error": "no_bridge",
                    "fix": "call stitchslop_connect first (token from the app's ⌁ Agent panel)"})
    try:
        reply = _bridge_http(session, "POST", "/call", {"command": command, "args": params})
    except Exception as exc:  # noqa: BLE001
        return _ok({"ok": False, "error": type(exc).__name__, "message": str(exc)})
    if isinstance(reply, dict):
        _persist(reply)
    return _ok(reply)


def _browser(args: dict | None = None, **_kw) -> str:
    a = args or {}
    action = str(a.get("action") or "status")
    overlay = a.get("overlay")
    overlay = None if overlay is None else bool(overlay)
    mod = browser_mod()
    if action == "start":
        return _ok(mod.ensure_relay(overlay=overlay))
    if overlay is not None:
        mod.set_overlay(overlay)
    if action == "stop":
        return _ok(mod.stop())
    return _ok(mod.status())


def _register_tool_compat(register, **kwargs) -> None:
    import inspect

    try:
        params = inspect.signature(register).parameters
    except (TypeError, ValueError):
        params = {}
    if params and not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    register(**kwargs)


def register(ctx) -> None:
    skills = _HERE / "skills"
    if skills.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(p for p in skills.iterdir() if p.is_dir()):
            if (child / "SKILL.md").is_file():
                ctx.register_skill(child.name, child / "SKILL.md")
    for schema, handler in ((STATUS_SCHEMA, _status), (CONNECT_SCHEMA, _connect),
                            (CALL_SCHEMA, _call), (BROWSER_SCHEMA, _browser)):
        _register_tool_compat(ctx.register_tool, name=schema["name"], toolset=TOOLSET,
                              schema=schema, handler=handler,
                              description=schema["description"], emoji="🧵")
