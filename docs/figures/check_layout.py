#!/usr/bin/env python3
"""Validate figure specs geometrically before they are rendered.

The renderer in the academic-architecture-diagram skill will happily draw text
that spills out of its box, nodes that sit on a panel border, and edges that
cross each other. Those defects are only visible to a pair of eyes, and the
point of this script is that a machine can catch them instead.

It checks, per spec:
  * labels are ASCII and no line exceeds 38 characters (the renderer's own rule)
  * every node lies inside a panel, with margin
  * a panel title is never overlapped by a node
  * no two nodes overlap
  * a label actually fits inside its node, measured with real font metrics
  * an edge starts and ends on the border of its source and target node
  * an edge label does not sit inside a node

Usage:
    python docs/figures/check_layout.py            # all specs
    python docs/figures/check_layout.py fig1       # one spec, by prefix

Exit code is 0 only when every spec passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.textpath import TextPath  # noqa: E402

SRC = Path(__file__).resolve().parent / "src"

# The renderer draws into axes that occupy this fraction of the figure width
# (fig.subplots_adjust(left=.015, right=.985)). Replicating it matters: a text
# box measured against the full figure width would be ~3% optimistic, which is
# exactly the size of the overflow this script exists to catch.
AXES_WIDTH_FRACTION = 0.985 - 0.015
PANEL_MARGIN = 0.12
TOL = 0.10


def text_size_inches(text: str, fontsize: float, bold: bool = False) -> tuple[float, float]:
    """Width and height of a (possibly multi-line) label, in inches."""
    prop = FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal")
    width = 0.0
    for line in text.splitlines() or [""]:
        if not line:
            continue
        ext = TextPath((0, 0), line, size=fontsize, prop=prop).get_extents()
        width = max(width, ext.width)
    lines = max(1, len(text.splitlines()))
    height = lines * fontsize * 1.28  # the renderer's linespacing
    return width / 72.0, height / 72.0


def data_per_inch(spec: dict) -> float:
    """How many data units one inch spans horizontally."""
    size = spec.get("size", [13, 6])
    canvas = spec.get("canvas", [16, 8])
    return canvas[0] / (size[0] * AXES_WIDTH_FRACTION)


def rect(node: dict) -> tuple[float, float, float, float]:
    return node["x"], node["y"], node["w"], node["h"]


def overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def contains(outer, inner, margin: float = 0.0) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox + margin - 1e-9
        and iy >= oy + margin - 1e-9
        and ix + iw <= ox + ow - margin + 1e-9
        and iy + ih <= oy + oh - margin + 1e-9
    )


def on_border(node: dict, point, tol: float = TOL) -> bool:
    x, y, w, h = rect(node)
    px, py = point
    inside_x = x - tol <= px <= x + w + tol
    inside_y = y - tol <= py <= y + h + tol
    if not (inside_x and inside_y):
        return False
    near_left = abs(px - x) <= tol
    near_right = abs(px - (x + w)) <= tol
    near_bottom = abs(py - y) <= tol
    near_top = abs(py - (y + h)) <= tol
    return near_left or near_right or near_bottom or near_top


def check_label(label: str, where: str, errors: list[str]) -> None:
    if any(ord(c) > 127 for c in label):
        errors.append(f"{where}: non-ASCII label {label!r} (renderer will refuse it)")
    for line in label.splitlines():
        if len(line) > 38:
            errors.append(f"{where}: line longer than 38 chars ({len(line)}): {line!r}")


def check_spec(path: Path) -> tuple[list[str], list[str]]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    notes: list[str] = []
    scale = data_per_inch(spec)

    pan = spec.get("panels", [])
    nodes = [n for n in spec.get("nodes", []) if True]
    by_id = {n["id"]: n for n in nodes}

    for p in pan:
        check_label(p["label"], f"{path.name} panel", errors)
    for n in nodes:
        check_label(n["label"], f"{path.name} node {n['id']}", errors)
    for e in spec.get("edges", []):
        if e.get("label"):
            check_label(e["label"], f"{path.name} edge {e.get('source')}->{e.get('target')}", errors)
    for a in spec.get("annotations", []):
        check_label(a["text"], f"{path.name} annotation", errors)

    # Nodes inside a panel, and not on its border.
    if pan:
        for n in nodes:
            r = rect(n)
            host = [p for p in pan if contains(rect(p), r)]
            if not host:
                snug = [p for p in pan if contains(rect(p), r, margin=PANEL_MARGIN)]
                if snug:
                    errors.append(
                        f"{path.name} node {n['id']}: only {PANEL_MARGIN} margin violated "
                        f"(sits on or too close to a panel border)")
                else:
                    errors.append(f"{path.name} node {n['id']}: not inside any panel")
            else:
                if not contains(rect(host[0]), r, margin=PANEL_MARGIN):
                    errors.append(
                        f"{path.name} node {n['id']}: closer than {PANEL_MARGIN} to the "
                        f"'{host[0]['label']}' panel border")

    # Panel titles must not be covered by a node.
    for p in pan:
        title_x = p["x"] + 0.18
        title_y = p["y"] + p["h"] - 0.28
        tw, th = text_size_inches(p["label"], p.get("font_size", 10), bold=True)
        tw_data, th_data = tw * scale, th * scale
        title_box = (title_x, title_y - th_data / 2, tw_data, th_data)
        for n in nodes:
            if overlaps(title_box, rect(n)):
                errors.append(
                    f"{path.name} node {n['id']} covers the '{p['label']}' panel title")

    # Node-node overlap.
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if overlaps(rect(a), rect(b)):
                errors.append(f"{path.name}: nodes {a['id']} and {b['id']} overlap")

    # Label fits inside its node, measured with real font metrics.
    for n in nodes:
        fs = n.get("font_size", 8.5)
        tw, th = text_size_inches(n["label"], fs)
        tw_data, th_data = tw * scale, th * scale
        inner_w = n["w"] - 0.10  # the box is drawn with pad=0.04
        inner_h = n["h"] - 0.10
        if tw_data > inner_w:
            errors.append(
                f"{path.name} node {n['id']}: label needs {tw_data:.2f} units but the node "
                f"is {inner_w:.2f} wide -> {n['label']!r}")
        if th_data > inner_h:
            errors.append(
                f"{path.name} node {n['id']}: label needs {th_data:.2f} units of height but "
                f"the node is {inner_h:.2f} tall")

    # An icon occupies the top-left corner; a label slid left can collide with it.
    for n in nodes:
        if not n.get("icon"):
            continue
        icon_box = (n["x"] + 0.04, n["y"] + n["h"] - 0.33, 0.28, 0.28)
        fs = n.get("font_size", 8.5)
        tw, th = text_size_inches(n["label"], fs)
        tw_data, th_data = tw * scale, th * scale
        label_box = (n["x"] + n["w"] / 2 - tw_data / 2,
                     n["y"] + n["h"] / 2 - th_data / 2, tw_data, th_data)
        if overlaps(icon_box, label_box):
            errors.append(f"{path.name} node {n['id']}: label overlaps its icon")

    # Edge endpoints must land on the node border they claim to leave from.
    for e in spec.get("edges", []):
        src, tgt = e.get("source"), e.get("target")
        if src not in by_id or tgt not in by_id:
            errors.append(f"{path.name}: edge references an unknown node ({src} -> {tgt})")
            continue
        pts = e.get("points")
        if pts:
            if len(pts) < 2:
                errors.append(f"{path.name}: edge {src}->{tgt} has fewer than two points")
                continue
            if not on_border(by_id[src], pts[0]):
                errors.append(
                    f"{path.name} edge {src}->{tgt}: start {pts[0]} is not on {src}'s border")
            if not on_border(by_id[tgt], pts[-1]):
                errors.append(
                    f"{path.name} edge {src}->{tgt}: end {pts[-1]} is not on {tgt}'s border")

    # Edge labels must not sit inside a node.
    for e in spec.get("edges", []):
        if not e.get("label") or not e.get("label_at"):
            continue
        fs = e.get("font_size", 7.5)
        tw, th = text_size_inches(e["label"], fs)
        tw_data, th_data = tw * scale, th * scale
        lx, ly = e["label_at"]
        box = (lx - tw_data / 2, ly - th_data / 2, tw_data, th_data)
        for n in nodes:
            if overlaps(box, rect(n)):
                errors.append(
                    f"{path.name} edge label {e['label']!r} sits inside node {n['id']}")

    notes.append(
        f"{path.name}: {len(nodes)} nodes, {len(pan)} panels, "
        f"{len(spec.get('edges', []))} edges, {scale:.2f} units/inch")
    return errors, notes


def main(argv: list[str]) -> int:
    only = argv[1] if len(argv) > 1 else None
    specs = sorted(SRC.glob("*.json"))
    if only:
        specs = [p for p in specs if p.name.startswith(only)]
    if not specs:
        print("no specs found", file=sys.stderr)
        return 2

    total_errors = 0
    for path in specs:
        errors, notes = check_spec(path)
        for note in notes:
            print(f"  {note}")
        for err in errors:
            print(f"  FAIL {err}")
        total_errors += len(errors)
        print(f"  {'PASS' if not errors else 'FAIL'} {path.name}\n")

    if total_errors:
        print(f"{total_errors} layout problem(s) found", file=sys.stderr)
        return 1
    print("all figure specs pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
