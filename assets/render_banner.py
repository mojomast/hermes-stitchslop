#!/usr/bin/env python3
"""Render assets/banner.html to assets/banner.png at exactly 1600x440 @2x.

Chrome's --screenshot honours the window size, not the page: the viewport it gives
you is shorter than --window-size, which silently crops or pads the banner. Setting
device metrics and clipping explicitly makes the output exact and repeatable.

    python3 assets/render_banner.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import browser  # noqa: E402

WIDTH, HEIGHT, SCALE = 1600, 440, 2


def main() -> int:
    port = browser.free_port()
    profile = tempfile.mkdtemp(prefix="banner-")
    proc = subprocess.Popen(
        [str(browser.chrome_binary()), "--headless=new", "--no-sandbox", "--disable-gpu",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "about:blank"],
        env=browser.chrome_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        target = browser.cdp_page(port, timeout=20)
        if not target:
            print("no page target", file=sys.stderr)
            return 1
        c = browser.WS(target["webSocketDebuggerUrl"])
        c.send({"id": 1, "method": "Emulation.setDeviceMetricsOverride",
                "params": {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": SCALE,
                           "mobile": False}})
        c.send({"id": 2, "method": "Page.navigate",
                "params": {"url": (HERE / "banner.html").as_uri()}})
        time.sleep(1.2)
        c.send({"id": 3, "method": "Page.captureScreenshot",
                "params": {"format": "png", "captureBeyondViewport": True,
                           "clip": {"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT,
                                    "scale": 1}}})   # deviceScaleFactor already applies
        for _ in range(60):
            msg = c.recv(timeout=6)
            if msg.get("id") == 3:
                data = msg["result"]["data"]
                (HERE / "banner.png").write_bytes(base64.b64decode(data))
                print(f"wrote {HERE / 'banner.png'} ({WIDTH * SCALE}x{HEIGHT * SCALE})")
                return 0
        print("no screenshot reply", file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
