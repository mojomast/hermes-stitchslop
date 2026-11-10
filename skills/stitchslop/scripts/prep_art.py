#!/usr/bin/env python3
"""Clean artwork before tracing it into stitches.

The tracer reproduces whatever you give it: white highlight slivers become holes,
a crop edge becomes a gouge, a subject smaller than its ring leaves a white halo.
This does the three fixes that matter, then writes a 2x PNG for a finer trace.

    python3 prep_art.py --in art.png --out art_clean.png \
        --max-hole 2500 --close-band 90:180 --target-radius 33.7 --ring-radius 33.8

Steps, in order:
  1. fill enclosed white components up to --max-hole px (specks, highlight holes);
     larger enclosed whites are kept, because a face is one of them
  2. optionally close a broken highlight band: for columns whose ink spans
     --close-band, fill white runs up to 34 px bounded by ink above and below
  3. optionally scale the subject about its centroid so its furthest pixel sits
     --target-radius, measured in mm on a --mm canvas
"""
from __future__ import annotations

import argparse
import math
from collections import deque
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - optional extra
    raise SystemExit(
        "prep_art.py needs NumPy and Pillow. They are not required by the core plugin. "
        "Install the optional extra: pip install 'stitchslop-hermes[art]' "
        "(or: pip install 'numpy>=1.26,<3' 'pillow>=10,<14')."
    ) from exc


def ink_mask(im: Image.Image) -> np.ndarray:
    a = np.asarray(im.convert("RGB")).astype(int)
    return (a[:, :, 2] - a[:, :, 0]) > 40


def fill_small_holes(mask: np.ndarray, max_hole: int) -> tuple[np.ndarray, list[int]]:
    probe = Image.fromarray((mask * 255).astype("uint8"), "L")
    ImageDraw.floodfill(probe, (0, 0), 128)
    arr = np.asarray(probe)
    holes = (arr != 128) & (arr != 255)
    h, w = holes.shape
    seen = np.zeros_like(holes)
    out = mask.copy()
    sizes: list[int] = []
    for y0, x0 in zip(*np.nonzero(holes)):
        if seen[y0, x0]:
            continue
        q = deque([(y0, x0)])
        seen[y0, x0] = True
        comp = []
        while q:
            y, x = q.popleft()
            comp.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and holes[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    q.append((ny, nx))
        sizes.append(len(comp))
        if len(comp) <= max_hole:
            for y, x in comp:
                out[y, x] = True
    return out, sizes


def close_band(mask: np.ndarray, y0: int, y1: int, max_gap: int = 34) -> np.ndarray:
    """Fill short white runs inside the subject, column by column, inside [y0,y1]."""
    out = mask.copy()
    h, w = mask.shape
    for x in range(w):
        col = mask[:, x]
        ys = np.nonzero(col)[0]
        if len(ys) < 2:
            continue
        top, bot = ys[0], ys[-1]
        if top > y1 or bot < y0:
            continue
        y = max(top, y0)
        limit = min(bot, y1)
        while y < limit:
            if not col[y]:
                j = y
                while j < limit and not col[j]:
                    j += 1
                if 2 <= j - y <= max_gap and y > top and j < bot:
                    out[y:j, x] = True
                y = j
            else:
                y += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--mm", type=float, default=89.0, help="canvas size the app will map this to")
    ap.add_argument("--size-px", type=int, default=1108, help="output resolution")
    ap.add_argument("--max-hole", type=int, default=2500, help="largest enclosed white to fill, px (at source scale)")
    ap.add_argument("--close-band", default="", help="y0:y1 pixel rows to run the band close in")
    ap.add_argument("--band-gap", type=int, default=34, help="longest white run to close, px")
    ap.add_argument("--target-radius", type=float, default=0.0, help="mm: scale so the furthest ink lands here")
    ap.add_argument("--fill", default="2b91f8", help="ink colour, hex")
    args = ap.parse_args()

    im = Image.open(args.src).convert("RGB")
    mask = ink_mask(im)
    mask, sizes = fill_small_holes(mask, args.max_hole)

    if args.close_band:
        y0, y1 = (int(v) for v in args.close_band.split(":"))
        before = int(mask.sum())
        mask = close_band(mask, y0, y1, args.band_gap)
        print(f"band close: +{int(mask.sum()) - before} px")

    h, w = mask.shape
    if args.target_radius > 0:
        ys, xs = np.nonzero(mask)
        cx, cy = w / 2, h / 2
        r = np.hypot(xs - cx, ys - cy) / (w / 2) * (args.mm / 2)
        factor = args.target_radius / r.max()
        sub = Image.fromarray((mask * 255).astype("uint8"), "L")
        bbox = sub.getbbox()
        crop = sub.crop(bbox)
        new = crop.resize((max(1, int(crop.width * factor)), max(1, int(crop.height * factor))), Image.LANCZOS)
        grown = Image.new("L", (w, h), 0)
        gx, gy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
        grown.paste(new, (int(gx - new.width / 2), int(gy - new.height / 2)))
        mask = np.asarray(grown) > 128
        print(f"scaled x{factor:.3f} so max radius is {args.target_radius}mm")

    big = Image.fromarray((mask * 255).astype("uint8"), "L").resize((args.size_px, args.size_px), Image.LANCZOS)
    colour = tuple(int(args.fill[i:i + 2], 16) for i in (0, 2, 4))
    out = Image.new("RGB", big.size, "white")
    out.paste(Image.new("RGB", big.size, colour), (0, 0), big)
    out.save(args.dst)

    ys, xs = np.nonzero(np.asarray(big) > 128)
    if len(xs):
        r = np.hypot(xs - args.size_px / 2, ys - args.size_px / 2) / (args.size_px / 2) * (args.mm / 2)
        print(f"ink radius {r.min():.1f}..{r.max():.1f} mm, bbox {xs.min()}..{xs.max()} x {ys.min()}..{ys.max()}")
    print(f"holes: {len(sizes)} components, filled px {int(mask.sum())}")
    print(f"wrote {args.dst} at {args.size_px}px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
