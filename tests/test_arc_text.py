"""The arc typesetter must fill the arc it was asked for, inside the band.

Regression for two mistakes made while building the badge:
  * the point-size solve overwrote the canvas size, so a 1108 px canvas came out 96 px;
  * a ring that only looked right because the source art was behind it.
"""
from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "stitchslop" / "scripts" / "arc_text.py"


def measure(png: Path, mm: float):
    im = Image.open(png).convert("L")
    a = np.asarray(im)
    h, w = a.shape
    ys, xs = np.where(a < 128)
    if not len(xs):
        return None
    r = np.hypot(xs - w / 2, ys - h / 2) / (w / 2) * (mm / 2)
    ang = (np.degrees(np.arctan2(ys - h / 2, xs - w / 2)) + 360) % 360
    bins = np.zeros(360)
    for value in ang:
        bins[int(value) % 360] += 1
    low = bins <= 0
    runs, i = [], 0
    while i < 360:
        if low[i]:
            j = i
            while j < 360 and low[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    if low[0] and low[-1] and len(runs) > 1:
        runs[0] = (runs[-1][0] - 360, runs[0][1])
        runs.pop()
    runs.sort(key=lambda t: t[1] - t[0], reverse=True)
    gap = runs[0][1] - runs[0][0] + 1 if runs else 0
    return {"r_min": float(r.min()), "r_max": float(r.max()), "coverage": 360 - gap,
            "size": im.size, "ink": len(xs)}


class ArcText(unittest.TestCase):
    def test_fills_the_requested_arc_inside_the_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ring.png"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT),
                 "--text", "REBELLION TO TYRANTS IS OBEDIENCE TO GOD",
                 "--out", str(out), "--band-in", "33.8", "--band-out", "42.3",
                 "--cap", "5.3", "--span", "308", "--start", "123", "--size-px", "1108"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(out.exists())
            m = measure(out, 89.0)
            self.assertIsNotNone(m, "nothing was drawn")
            self.assertEqual(m["size"], (1108, 1108), "the canvas must keep --size-px")
            self.assertGreaterEqual(m["r_min"], 33.8 - 0.8)
            self.assertLessEqual(m["r_max"], 42.3 + 0.8)
            self.assertAlmostEqual(m["coverage"], 308, delta=6)

    def test_short_text_still_centres(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "short.png"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--text", "HELLO", "--out", str(out),
                 "--band-in", "30", "--band-out", "40", "--cap", "5", "--span", "120",
                 "--start", "90", "--size-px", "800"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            m = measure(out, 89.0)
            self.assertIsNotNone(m)
            self.assertLessEqual(m["r_max"], 40.5)
            self.assertGreater(m["coverage"], 60)


if __name__ == "__main__":
    unittest.main()
