#!/usr/bin/env python3
"""Typeset a sentence onto a circular arc and save it as a PNG the app can trace.

The catalog font in Stitch Slop is single-line: at any cap height that fits an
arc, its stems are thinner than one stitch and it sews as hairlines. This renders
real outlines from a bold condensed face at the exact arc geometry instead, which
`background.digitize` turns into solid satin letters.

    python3 arc_text.py --text "REBELLION TO TYRANTS" --out ring.png \
        --band-in 33.8 --band-out 42.3 --span 308 --start 123 --mm 89

Prints the solved cap height and the measured ink radii; open the PNG and check
orientation and spelling before tracing — that is the cheap place to catch both.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - optional extra
    raise SystemExit(
        "arc_text.py needs Pillow. It is not required by the core plugin. "
        "Install the optional extra: pip install 'stitchslop-hermes[art]' "
        "(or: pip install 'pillow>=10,<14')."
    ) from exc

DEFAULT_FONTS = (
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
)


def find_font(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in DEFAULT_FONTS:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("no bold condensed font found; pass --font")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", default="ring-text.png")
    ap.add_argument("--font")
    ap.add_argument("--mm", type=float, default=89.0, help="canvas size in mm (match background.set)")
    ap.add_argument("--size-px", type=int, default=1108, help="canvas resolution")
    ap.add_argument("--band-in", type=float, default=33.8, help="inner ring radius, mm")
    ap.add_argument("--band-out", type=float, default=42.3, help="outer ring radius, mm")
    ap.add_argument("--cap", type=float, default=5.3, help="desired cap height, mm")
    ap.add_argument("--span", type=float, default=308.0, help="degrees of arc the sentence should fill")
    ap.add_argument("--start", type=float, default=123.0, help="start angle: 0=right, 90=down, 180=left")
    ap.add_argument("--center", type=float, default=None,
                    help="centre the sentence on this angle instead of starting at --start (270=top, 90=bottom)")
    ap.add_argument("--direction", choices=("cw", "ccw"), default="cw",
                    help="cw reads clockwise (top arcs); ccw keeps bottom-arc text upright")
    ap.add_argument("--max-cap", type=float, default=0.0,
                    help="mm ceiling on the cap (default: 85%% of the band). Short text would otherwise grow past the band to fill the arc")
    ap.add_argument("--tracking", type=float, default=0.03, help="letter spacing, em")
    ap.add_argument("--space", type=float, default=0.62, help="extra word gap, em")
    args = ap.parse_args()

    text = args.text.upper()
    canvas_px = args.size_px
    px_per_mm = canvas_px / args.mm
    cx = cy = canvas_px / 2

    font_path = find_font(args.font)
    probe = ImageFont.truetype(font_path, 10)
    upem = getattr(getattr(probe, "font", None), "units_per_em", None) or 2048

    def advance(ch: str, ref: ImageFont.FreeTypeFont, scale: float) -> float:
        extra = args.space * upem if ch == " " else 0
        return ref.getlength(ch) / scale + args.tracking * upem + extra

    # radius depends on the cap, the cap on the point size, the size on the radius.
    # Solve for the point size at a given baseline radius, then re-center the band
    # around the cap that size actually produced; a couple of passes converge.
    cap_ratio = 0.72                       # cap height as a fraction of the em
    max_cap = args.max_cap or 0.85 * (args.band_out - args.band_in)
    max_cap_px = max_cap * px_per_mm

    def solve(radius_mm: float) -> tuple[int, float, float]:
        """(point size, cap px, span degrees) for a target span at this radius."""
        font_px = canvas_px
        f = ImageFont.truetype(font_path, font_px)
        for _ in range(60):
            adv_scale = font_px / upem
            units_total = sum(advance(c, f, adv_scale) for c in text)
            span = math.degrees(units_total * adv_scale / (radius_mm * px_per_mm))
            font_px = max(8, int(round(font_px * (args.span / span) ** 0.5)))
            f = ImageFont.truetype(font_path, font_px)
        # the span solve converges first; only then does the cap ceiling apply, or
        # the first (oversized) iteration would clamp and never solve the arc
        if font_px * cap_ratio > max_cap_px:
            font_px = max(8, int(max_cap_px / cap_ratio))
            f = ImageFont.truetype(font_path, font_px)
        adv_scale = font_px / upem
        units_total = sum(advance(c, f, adv_scale) for c in text)
        span = math.degrees(units_total * adv_scale / (radius_mm * px_per_mm))
        return font_px, font_px * cap_ratio, span

    def baseline_for(cap_mm: float) -> float:
        """Baseline radius that centres the cap in the band for this direction.

        Clockwise letters stand on the baseline and grow outward; counter-clockwise
        (bottom-arc) letters hang above it and grow inward, so the baseline is the
        outer edge of the band instead of the inner one.
        """
        margin = (args.band_out - args.band_in - cap_mm) / 2
        return args.band_out - margin if args.direction == "ccw" else args.band_in + margin

    radius = baseline_for(args.cap)
    for _ in range(3):
        font_px, cap_px, span_deg = solve(radius)
        radius = baseline_for(cap_px / px_per_mm)
    f = ImageFont.truetype(font_path, font_px)
    adv_scale = font_px / upem

    canvas = Image.new("L", (canvas_px, canvas_px), 255)
    total_angle = sum(advance(c, f, adv_scale) for c in text) * adv_scale / (radius * px_per_mm)
    if args.center is not None:
        spin = -1 if args.direction == "cw" else 1
        start_rad = math.radians(args.center) + spin * total_angle / 2
    else:
        start_rad = math.radians(args.start)
    dist = 0.0
    placed = 0
    for ch in text:
        adv = advance(ch, f, adv_scale)
        if ch != " ":
            offset = (dist + adv / 2) * adv_scale / (radius * px_per_mm)
            if args.direction == "cw":
                theta = start_rad + offset
                upright = math.degrees(theta) + 90
            else:
                theta = start_rad - offset
                upright = math.degrees(theta) - 90
            tile_px = max(16, int(font_px * 1.6))
            tile = Image.new("L", (tile_px, tile_px), 0)
            ImageDraw.Draw(tile).text((tile_px / 2, tile_px / 2), ch, font=f, fill=255, anchor="ms")
            tile = tile.rotate(-upright, resample=Image.BICUBIC, fillcolor=0)
            px = cx + radius * px_per_mm * math.cos(theta)
            py = cy + radius * px_per_mm * math.sin(theta)
            canvas.paste(0, (int(px - tile_px / 2), int(py - tile_px / 2)), tile)
            placed += 1
        dist += adv

    canvas.convert("RGB").save(args.out)

    import numpy as np
    a = np.array(canvas)
    ys, xs = np.where(a < 128)
    if len(xs):
        r = np.hypot(xs - cx, ys - cy) / (canvas_px / 2) * (args.mm / 2)
        print(f"glyphs {placed}  font {Path(font_path).name}  point size {font_px}px on {canvas_px}px canvas")
        print(f"cap {cap_px / px_per_mm:.2f}mm  baseline r {radius:.2f}mm  ink r {r.min():.1f}..{r.max():.1f}mm")
        print(f"direction {args.direction}  span {math.degrees(total_angle):.1f}deg")
    else:
        print("WARNING: nothing drawn — check --text and the font")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
