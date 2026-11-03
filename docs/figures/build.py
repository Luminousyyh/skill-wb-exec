#!/usr/bin/env python3
"""Rebuild the figures from their JSON sources, reproducibly.

The renderer in the academic-architecture-diagram skill writes PNGs at dpi=220,
which is ~2475 px wide for these canvases. The files committed here are 1280 px
wide, and that downscale used to be an unrecorded manual step -- so anyone who
re-ran the renderer got a different size and a different file weight than what
the repository holds. This script performs both steps, in order, so the output
is deterministic.

It refuses to run if it cannot do the downscale: a 2475 px PNG silently
replacing a 1280 px one is worse than an error.

Usage:
    python docs/figures/build.py              # every spec
    python docs/figures/build.py fig1         # one spec, by prefix
    python docs/figures/build.py --check-only # verify specs without writing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
TARGET_WIDTH = 1280

RENDERER_CANDIDATES = [
    Path.home() / ".agents/skills/academic-architecture-diagram/scripts/render_diagram.py",
    Path.home() / ".codex/skills/academic-architecture-diagram/scripts/render_diagram.py",
    Path.home() / ".claude/skills/academic-architecture-diagram/scripts/render_diagram.py",
]


def find_renderer() -> Path:
    override = os.environ.get("WB_FIGURE_RENDERER")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise SystemExit(f"WB_FIGURE_RENDERER points at a missing file: {p}")
        return p
    for candidate in RENDERER_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "cannot find render_diagram.py. Install the academic-architecture-diagram "
        "skill, or set WB_FIGURE_RENDERER to its scripts/render_diagram.py"
    )


def require_pillow():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "Pillow is required to downscale the PNGs to "
            f"{TARGET_WIDTH}px. Without it the build would emit 2475px images "
            "that do not match the repository. Install Pillow, or run with "
            "--check-only to validate specs without writing."
        ) from exc
    from PIL import Image

    return Image


def render_one(renderer: Path, spec: Path) -> Path:
    out = HERE / spec.stem
    subprocess.run(
        [sys.executable, str(renderer), "--spec", str(spec), "--out", str(out)],
        check=True,
    )
    return out


def downscale(png: Path, Image) -> tuple[int, int, int]:
    before = png.stat().st_size
    with Image.open(png) as im:
        im = im.convert("RGB")
        width, height = im.size
        if width != TARGET_WIDTH:
            height = round(height * TARGET_WIDTH / width)
            im = im.resize((TARGET_WIDTH, height), Image.LANCZOS)
        im.save(png, format="PNG", optimize=True)
        final = im.size
    return final[0], final[1], png.stat().st_size - before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="?", help="spec prefix, e.g. fig1")
    ap.add_argument("--check-only", action="store_true",
                    help="validate specs only; render and write nothing")
    args = ap.parse_args()

    specs = sorted(SRC.glob("*.json"))
    if args.only:
        specs = [p for p in specs if p.stem.startswith(args.only)]
    if not specs:
        print("no specs matched", file=sys.stderr)
        return 2

    # Layout first: a spec that would render text outside its box should never
    # reach the renderer.
    check = subprocess.run(
        [sys.executable, str(HERE / "check_layout.py")] + ([args.only] if args.only else []),
        text=True,
    )
    if check.returncode != 0:
        print("layout check failed; refusing to render", file=sys.stderr)
        return 1
    if args.check_only:
        return 0

    Image = require_pillow()
    renderer = find_renderer()
    print(f"renderer: {renderer}")

    for spec in specs:
        out = render_one(renderer, spec)
        png = out.with_suffix(".png")
        w, h, delta = downscale(png, Image)
        size_kb = png.stat().st_size / 1024
        print(f"  {png.name}: {w}x{h}, {size_kb:.1f} KB, {delta:+,} bytes after downscale")

    print(f"\nrebuilt {len(specs)} figure(s) at {TARGET_WIDTH}px wide")
    return 0


if __name__ == "__main__":
    sys.exit(main())
