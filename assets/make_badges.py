#!/usr/bin/env python3
"""Emit the small static badges used in the README.

Shields.io is fine but three badges failed to load through GitHub's image proxy,
and a broken badge looks worse than no badge. These are local SVG, same shape.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent / "badges"
HERE.mkdir(parents=True, exist_ok=True)

BADGES = {
    "hermes": ("hermes", "≥ 0.21.6", 62, 62),
    "tools": ("tools", "4", 44, 22),
    "deps": ("deps", "none", 52, 34),
}


def main() -> int:
    for name, (label, value, lw, vw) in BADGES.items():
        total = lw + vw
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{lw}" height="20" fill="#555"/>
    <rect x="{lw}" width="{vw}" height="20" fill="#2b91f8"/>
    <rect width="{total}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">
    <text x="{lw / 2:.0f}" y="14">{label}</text>
    <text x="{lw + vw / 2:.0f}" y="14">{value}</text>
  </g>
</svg>
'''
        (HERE / f"{name}.svg").write_text(svg)
        print("wrote", HERE / f"{name}.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
