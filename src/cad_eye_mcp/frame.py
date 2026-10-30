"""Compose what the AI sees: CADCraft's render of the current view, framed by coordinate
rulers, a light grid, selection highlights, markers, a minimap and a status bar."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import paths
from .view import RULER_LEFT, RULER_TOP, BBox, Point, View

PANEL = (21, 25, 31)
RULER_TEXT = (170, 178, 189)
GRID = (255, 255, 255, 30)
HILITE = (255, 61, 245)
MARKER = (255, 166, 0)
MINIMAP_W = 200

_fonts: dict[int, ImageFont.ImageFont] = {}


def font(size: int) -> ImageFont.ImageFont:
    if size not in _fonts:
        f = paths.overlay_font()
        try:
            _fonts[size] = ImageFont.truetype(str(f[0]), size, index=f[1]) if f else ImageFont.load_default(size)
        except OSError:
            _fonts[size] = ImageFont.load_default(size)
    return _fonts[size]


def nice_step(raw: float) -> float:
    """The smallest 1-2-5 step that is at least `raw`."""
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    exp = math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * 10 ** exp >= raw:
            return m * 10 ** exp
    return 10 ** (exp + 1)


def fmt(v: float, step: float) -> str:
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    s = f"{v:.{decimals}f}"
    return "0" if s.strip("-0.") == "" else s


@dataclass
class Overlay:
    highlights: list[list[np.ndarray]] = field(default_factory=list)   # world polylines, per entity
    markers: list[tuple[Point, str]] = field(default_factory=list)
    boxes: list[BBox] = field(default_factory=list)
    lines: list[list[Point]] = field(default_factory=list)              # e.g. a measurement path


def _ticks(lo: float, hi: float, step: float) -> list[float]:
    first = math.ceil(lo / step)
    return [i * step for i in range(first, int(math.floor(hi / step)) + 1)]


def compose(view: View, render_png: bytes, overlay: Overlay, status: list[str],
            minimap: tuple[Image.Image, View] | None = None, grid: bool = True) -> bytes:
    fw, fh = view.frame_size
    aw, ah = view.width_px, view.height_px
    (x0, y0), (x1, y1) = view.window
    step = nice_step(view.scale * 90)
    xs, ys = _ticks(x0, x1, step), _ticks(y0, y1, step)

    # 1. drawing area: render + grid + highlights + markers, clipped to the area
    area = Image.open(io.BytesIO(render_png)).convert("RGBA")
    if area.size != (aw, ah):
        area = area.resize((aw, ah))
    top = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    if grid:
        for x in xs:
            px = (x - x0) / view.scale
            d.line([(px, 0), (px, ah)], fill=GRID)
        for y in ys:
            py = (y1 - y) / view.scale
            d.line([(0, py), (aw, py)], fill=GRID)

    def to_area(pts: np.ndarray) -> list[tuple[float, float]]:
        ax = (pts[:, 0] - x0) / view.scale
        ay = (y1 - pts[:, 1]) / view.scale
        return list(zip(ax.tolist(), ay.tolist()))

    for shapes in overlay.highlights:
        pxs = [to_area(s) for s in shapes if len(s)]
        if not pxs:
            continue
        allp = np.asarray([p for ps in pxs for p in ps])
        for ps in pxs:
            if len(ps) >= 2:
                d.line(ps, fill=HILITE + (235,), width=3, joint="curve")
        if float(np.ptp(allp, axis=0).max()) < 14:  # tiny on screen: ring it so it can't be missed
            cx, cy = allp.mean(axis=0)
            d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], outline=HILITE + (255,), width=2)

    for (bx0, by0), (bx1, by1) in overlay.boxes:
        a = to_area(np.asarray([[bx0, by1], [bx1, by0]]))
        d.rectangle([a[0], a[1]], outline=MARKER + (220,), width=1)

    for path in overlay.lines:
        if len(path) >= 2:
            d.line(to_area(np.asarray(path)), fill=MARKER + (255,), width=2)

    for (wx, wy), label in overlay.markers:
        (px, py), = to_area(np.asarray([[wx, wy]]))
        d.line([(px - 11, py), (px + 11, py)], fill=MARKER + (255,), width=2)
        d.line([(px, py - 11), (px, py + 11)], fill=MARKER + (255,), width=2)
        if label:
            d.text((px + 9, py - 22), label, font=font(14), fill=MARKER + (255,),
                   stroke_width=3, stroke_fill=PANEL + (255,))

    area = Image.alpha_composite(area, top).convert("RGB")

    if minimap is not None:
        thumb, tview = minimap
        mx, my = aw - thumb.width - 8, 8
        area.paste(thumb, (mx, my))
        md = ImageDraw.Draw(area)
        md.rectangle([mx - 1, my - 1, mx + thumb.width, my + thumb.height], outline=RULER_TEXT)
        (vx0, vy0), (vx1, vy1) = view.window
        ax, ay = tview.world_to_area(vx0, vy1)
        bx, by = tview.world_to_area(vx1, vy0)
        ax, bx = max(ax, 0), min(bx, thumb.width - 1)
        ay, by = max(ay, 0), min(by, thumb.height - 1)
        if bx - ax < 4:
            ax, bx = (ax + bx) / 2 - 2, (ax + bx) / 2 + 2
        if by - ay < 4:
            ay, by = (ay + by) / 2 - 2, (ay + by) / 2 + 2
        md.rectangle([mx + ax, my + ay, mx + bx, my + by], outline=MARKER, width=2)

    # 2. frame: rulers and status bar around the area
    frame = Image.new("RGB", (fw, fh), PANEL)
    frame.paste(area, (RULER_LEFT, RULER_TOP))
    fd = ImageDraw.Draw(frame)
    small = font(12)
    for x in xs:
        px = RULER_LEFT + (x - x0) / view.scale
        fd.line([(px, RULER_TOP - 6), (px, RULER_TOP - 1)], fill=RULER_TEXT)
        label = fmt(x, step)
        tw = fd.textlength(label, font=small)
        fd.text((min(max(px - tw / 2, RULER_LEFT), fw - tw - 2), 3), label, font=small, fill=RULER_TEXT)
    for y in ys:
        py = RULER_TOP + (y1 - y) / view.scale
        fd.line([(RULER_LEFT - 6, py), (RULER_LEFT - 1, py)], fill=RULER_TEXT)
        label = fmt(y, step)
        tw = fd.textlength(label, font=small)
        fd.text((RULER_LEFT - 8 - tw, min(max(py - 8, RULER_TOP), RULER_TOP + ah - 16)), label,
                font=small, fill=RULER_TEXT)
    for i, line in enumerate(status[:2]):
        fd.text((8, RULER_TOP + ah + 5 + i * 19), line, font=font(13), fill=(225, 230, 236))

    buf = io.BytesIO()
    frame.save(buf, "PNG", compress_level=6)
    return buf.getvalue()
