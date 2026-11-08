"""Geometric validation for the committed figure PNGs.

Why this exists: a diagram can be wrong in ways only an eye would normally
catch. The concrete defect that motivated this check was a two-panel figure
whose panels were supposed to be stacked but never moved — the canvas height had
doubled, every text element stayed inside the canvas, and the renderer reported
zero errors, yet 59% of the image was empty white and the panels were still side
by side.

So this measures the thing that was actually wrong: the bounding box of the
non-white pixels. It refuses a figure whose blank margin exceeds 35% on any side,
whose content covers less than 55% of either axis, or that contains no ink at
all.

Requires Pillow (development only; the product itself is standard library only).

Usage:
    python docs/figures/pixel_gate.py          # every fig*.png next to this file
    python docs/figures/pixel_gate.py fig4     # one figure, by prefix
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover
    raise SystemExit("Pillow is required to validate the figures: pip install Pillow")

HERE = Path(__file__).resolve().parent
INK_THRESHOLD = 248   # any channel below this counts as ink
MARGIN_LIMIT = 0.35   # >35% blank on one side is a defect
COVER_LIMIT = 0.55    # content bbox must cover >=55% of each axis


def measure(png: Path) -> dict:
    with Image.open(png) as im:
        im = im.convert("RGB")
        width, height = im.size
        r, g, b = im.split()
        # per-pixel minimum channel, so a pale fill still counts as ink
        mn = ImageChops.darker(ImageChops.darker(r, g), b)
        mask = mn.point(lambda v: 255 if v < INK_THRESHOLD else 0)
        ink = mask.histogram()[255]
        box = mask.getbbox()

    if box is None or ink == 0:
        return {"name": png.name, "size": (width, height), "ink_pct": 0.0,
                "blank": None, "flags": ["blank-image"]}

    x0, y0, x1, y1 = box
    blank = {"top": y0 / height, "bottom": (height - y1) / height,
             "left": x0 / width, "right": (width - x1) / width}
    cover_w, cover_h = (x1 - x0) / width, (y1 - y0) / height

    flags = [f"blank-band:{side}={ratio:.0%}" for side, ratio in blank.items()
             if ratio > MARGIN_LIMIT]
    if cover_w < COVER_LIMIT:
        flags.append(f"content-narrow:width={cover_w:.0%}")
    if cover_h < COVER_LIMIT:
        flags.append(f"content-short:height={cover_h:.0%}")

    return {"name": png.name, "size": (width, height),
            "ink_pct": round(100.0 * ink / (width * height), 1),
            "blank": {k: f"{v:.0%}" for k, v in blank.items()},
            "flags": flags}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    pngs = sorted(p for p in HERE.glob("fig*.png"))
    if only:
        pngs = [p for p in pngs if p.name.startswith(only)]
    if not pngs:
        print("no figure PNGs found")
        return 1

    print(f"{'figure':<28}{'size':<12}{'ink%':>6}  margins (t/b/l/r)   verdict")
    print("-" * 84)
    failed = []
    for png in pngs:
        info = measure(png)
        m = info["blank"]
        v = "PASS" if not info["flags"] else "FAIL  " + ", ".join(info["flags"])
        w, h = info["size"]
        margins = f"{m['top']}/{m['bottom']}/{m['left']}/{m['right']}" if m else "-"
        print(f"{png.stem:<28}{f'{w}x{h}':<12}{info['ink_pct']:>6.1f}  "
              f"{margins:<19} {v}")
        if info["flags"]:
            failed.append(png.name)

    print("-" * 84)
    if failed:
        print(f"{len(pngs) - len(failed)}/{len(pngs)} pass; FAILED: {', '.join(failed)}")
        return 1
    print(f"{len(pngs)}/{len(pngs)} pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
