"""The viewport: which part of the drawing is on screen, and pixel <-> drawing coordinates.

A frame image is the drawing area plus rulers (left/top) and a status bar (bottom). Pixel
coordinates given to tools are always pixels of the *whole frame image* the AI was shown."""

from __future__ import annotations

from dataclasses import dataclass, replace

Point = tuple[float, float]
BBox = tuple[Point, Point]

RULER_LEFT = 72
RULER_TOP = 24
STATUS_H = 44


@dataclass(frozen=True)
class View:
    cx: float
    cy: float
    height: float          # drawing units shown vertically
    width_px: int = 1200   # drawing area size in pixels
    height_px: int = 760

    @property
    def scale(self) -> float:
        """Drawing units per pixel."""
        return self.height / self.height_px

    @property
    def width(self) -> float:
        return self.scale * self.width_px

    @property
    def window(self) -> BBox:
        hw, hh = self.width / 2, self.height / 2
        return (self.cx - hw, self.cy - hh), (self.cx + hw, self.cy + hh)

    @property
    def frame_size(self) -> tuple[int, int]:
        return RULER_LEFT + self.width_px, RULER_TOP + self.height_px + STATUS_H

    # -- coordinates -------------------------------------------------------------------
    def world_to_area(self, x: float, y: float) -> tuple[float, float]:
        (x0, _), (_, y1) = self.window
        return (x - x0) / self.scale, (y1 - y) / self.scale

    def world_to_frame(self, x: float, y: float) -> tuple[float, float]:
        ax, ay = self.world_to_area(x, y)
        return ax + RULER_LEFT, ay + RULER_TOP

    def frame_to_world(self, px: float, py: float) -> Point:
        (x0, _), (_, y1) = self.window
        return x0 + (px - RULER_LEFT) * self.scale, y1 - (py - RULER_TOP) * self.scale

    def to_world(self, x: float, y: float, coords: str) -> Point:
        if coords == "world":
            return float(x), float(y)
        if coords == "px":
            return self.frame_to_world(x, y)
        raise ValueError(f"coords must be 'px' or 'world', not {coords!r}")

    # -- navigation --------------------------------------------------------------------
    def zoom(self, factor: float, anchor: Point | None = None) -> "View":
        """factor > 1 zooms in. The anchor point stays at the same place on screen."""
        if factor <= 0:
            raise ValueError("factor must be positive")
        ax, ay = anchor if anchor else (self.cx, self.cy)
        return replace(self, cx=ax + (self.cx - ax) / factor, cy=ay + (self.cy - ay) / factor,
                       height=self.height / factor)

    def pan(self, dx: float, dy: float) -> "View":
        return replace(self, cx=self.cx + dx, cy=self.cy + dy)

    def fit(self, bbox: BBox, margin: float = 0.05) -> "View":
        return fit_view(bbox, self.width_px, self.height_px, margin)

    def resized(self, width_px: int, height_px: int) -> "View":
        return replace(self, width_px=width_px, height_px=height_px,
                       height=self.height * height_px / self.height_px)


def fit_view(bbox: BBox, width_px: int, height_px: int, margin: float = 0.05) -> View:
    (x0, y0), (x1, y1) = bbox
    w, h = max(x1 - x0, 0.0), max(y1 - y0, 0.0)
    if w == 0 and h == 0:
        w = h = 1.0
    aspect = width_px / height_px
    height = max(h, w / aspect) * (1 + 2 * margin)
    return View((x0 + x1) / 2, (y0 + y1) / 2, height, width_px, height_px)


def normalize_box(p: Point, q: Point) -> BBox:
    return (min(p[0], q[0]), min(p[1], q[1])), (max(p[0], q[0]), max(p[1], q[1]))
