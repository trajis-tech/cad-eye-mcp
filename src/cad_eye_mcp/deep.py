"""Exact drawing data from ezdxf: entity details, geometry for highlighting and picking,
lengths and areas. CADCraft keeps the original DXF handles, so both engines share them."""

from __future__ import annotations

import math
import mmap
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import ezdxf
import numpy as np
from ezdxf import bbox as ezbbox
from ezdxf import path as ezpath
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic
from ezdxf.lldxf.encoding import decode_dxf_unicode

from .view import BBox, Point

UNIT_NAMES = {0: "unitless", 1: "in", 2: "ft", 3: "mi", 4: "mm", 5: "cm", 6: "m", 7: "km",
              8: "µin", 9: "mil", 10: "yd", 11: "Å", 12: "nm", 13: "µm", 14: "dm",
              15: "dam", 16: "hm", 17: "Gm", 18: "AU", 19: "ly", 20: "pc"}
INFINITE = {"XLINE", "RAY"}
TEXTUAL = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
TABLE = "ACAD_TABLE"  # ezdxf reads tables as tag storage, not as graphic entities
FLATTEN = 64  # segments per full circle when flattening curves for picking/highlighting


def load(path: Path) -> tuple[Drawing, list[str]]:
    """Read a DXF, falling back to ezdxf's recover mode for damaged files."""
    try:
        return ezdxf.readfile(path), []
    except ezdxf.DXFStructureError:
        from ezdxf import recover

        doc, auditor = recover.readfile(path)
        return doc, [f"recovered damaged DXF ({len(auditor.fixes)} fixes, {len(auditor.errors)} errors)"]


def has_handles(path: Path) -> bool:
    """Whether a DXF numbers its entities ($HANDSEED in the header). Old R12 writers (I-DEAS,
    many CAM tools) leave handles out, and then every reader invents its own."""
    with open(path, "rb") as f:
        if f.seek(0, 2) == 0:
            return False
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            end = m.find(b"ENDSEC")
            return m.find(b"$HANDSEED", 0, end if end >= 0 else len(m)) >= 0


def units(doc: Drawing) -> str:
    return UNIT_NAMES.get(doc.header.get("$INSUNITS", 0), "unitless")


def extents(entities: Iterable[DXFGraphic]) -> BBox | None:
    """Extents ignoring infinite lines (XLINE/RAY), which otherwise blow the view up, and entities
    whose geometry cannot be computed (e.g. damaged proxy graphics)."""
    entities = [e for e in entities if e.dxftype() not in INFINITE]
    try:
        box = ezbbox.extents(entities, fast=True)
    except Exception:  # noqa: BLE001 - one bad entity; measure the rest one by one
        box = ezbbox.BoundingBox()
        for e in entities:
            try:
                one = ezbbox.extents([e], fast=True)
            except Exception:  # noqa: BLE001
                continue
            if one.has_data:
                box.extend([one.extmin, one.extmax])
    if not box.has_data:
        return None
    return (box.extmin.x, box.extmin.y), (box.extmax.x, box.extmax.y)


def entity_bbox(e: DXFGraphic) -> BBox | None:
    if e.dxftype() == TABLE:
        try:
            return extents(e.virtual_entities())
        except Exception:  # noqa: BLE001
            return None
    if e.dxftype() in TEXTUAL:
        box = text_box(e)
        if box is not None:
            return (float(box[:, 0].min()), float(box[:, 1].min())), (float(box[:, 0].max()), float(box[:, 1].max()))
    return extents([e])


def _em_width(s: str) -> float:
    """Text advance in multiples of the text height. CAD text height is the capital height, so
    a full-width CJK glyph advances about 1.1-1.5 h depending on the font (measured: Noto Sans
    CJK ~1.1, MingLiU/MS Gothic ~1.5); we take the wide end so a click on any part of the text
    still hits it."""
    return sum(1.5 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.75 if ch == " " else 0.8
               for ch in s)


_HEIGHT_CODE = re.compile(r"\\H([0-9.]+)(x?);")


def _paragraph_heights(raw: str, base: float) -> list[float]:
    """Largest text height in each MTEXT paragraph, following inline \\H codes (\\H2x; scales
    the current height, \\H5; sets it). Brace scoping is ignored: this is an estimate."""
    out, cur = [], base
    for para in raw.split("\\P"):
        tallest = cur
        for m in _HEIGHT_CODE.finditer(para):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            cur = cur * v if m.group(2) else v
            tallest = max(tallest, cur)
        out.append(tallest)
    return out


def _wrapped_lines(line: str, h: float, column: float) -> int:
    """Lines a paragraph takes when word-wrapped at `column` (CJK text without spaces wraps
    anywhere, which the character-width bound covers)."""
    space = _em_width(" ") * h
    n, cur = 1, 0.0
    for word in line.split(" "):
        ww = _em_width(word) * h
        if cur and cur + space + ww > column:
            n, cur = n + 1, ww
        else:
            cur += (space if cur else 0.0) + ww
    return max(n, math.ceil(_em_width(line) * h / column))


# TEXT alignment (halign, valign) -> fraction of width / height to shift the box by
_H_SHIFT = {0: 0.0, 1: -0.5, 2: -1.0, 3: 0.0, 4: -0.5, 5: 0.0}
_V_SHIFT = {0: 0.0, 1: -0.2, 2: -0.5, 3: -1.0}
# MTEXT attachment point 1..9 -> (x shift, y shift) as fractions of the box
_MTEXT_SHIFT = {1: (0, -1), 2: (-0.5, -1), 3: (-1, -1), 4: (0, -0.5), 5: (-0.5, -0.5), 6: (-1, -0.5),
                7: (0, 0), 8: (-0.5, 0), 9: (-1, 0)}


def text_box(e: DXFGraphic) -> np.ndarray | None:
    """Closed outline of a TEXT/ATTRIB/MTEXT, estimated with CJK-aware character widths
    (ezdxf's own estimate assumes narrow glyphs and cuts Chinese/Japanese text short)."""
    dx = e.dxf
    try:
        if e.dxftype() == "MTEXT":
            lines = plain(e).split("\n") or [""]
            heights = _paragraph_heights(e.text, dx.char_height)
            if len(heights) != len(lines):
                heights = [dx.char_height] * len(lines)
            widths = [_em_width(l) * h for l, h in zip(lines, heights)]
            w = max(widths)
            column = dx.get("width", 0.0)
            counts = [1] * len(lines)
            if column > 0 and w > column:  # paragraphs wrap at the column width
                w = column
                counts = [_wrapped_lines(l, h, column) for l, h in zip(lines, heights)]
            # AutoCAD's default line pitch is 5/3 of the character height
            pitch = 5 / 3 * dx.get("line_spacing_factor", 1.0)
            total_h = sum(n * h * pitch for n, h in zip(counts, heights)) - heights[-1] * (pitch - 1)
            sx, sy = _MTEXT_SHIFT.get(dx.attachment_point, (0, -1))
            origin = dx.insert
            angle = math.radians(dx.get("rotation", 0.0))
            if dx.hasattr("text_direction"):
                angle = math.atan2(dx.text_direction.y, dx.text_direction.x)
            local = [(sx * w, sy * total_h), ((sx + 1) * w, sy * total_h),
                     ((sx + 1) * w, (sy + 1) * total_h), (sx * w, (sy + 1) * total_h)]
        else:
            text = plain(e)
            h = dx.height
            w = _em_width(text) * h * dx.get("width", 1.0)
            halign, valign = dx.get("halign", 0), dx.get("valign", 0)
            align_point = halign != 0 or valign != 0
            origin = dx.align_point if align_point and dx.hasattr("align_point") else dx.insert
            if halign in (3, 5):  # ALIGNED / FIT stretch between insert and align point
                origin = dx.insert
                w = max(w, dx.insert.distance(dx.align_point)) if dx.hasattr("align_point") else w
            angle = math.radians(dx.get("rotation", 0.0))
            x0 = _H_SHIFT.get(halign, 0.0) * w
            y0 = _V_SHIFT.get(valign, 0.0) * h - (0.2 * h if valign == 0 else 0)
            local = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + 1.2 * h), (x0, y0 + 1.2 * h)]
    except Exception:  # noqa: BLE001
        return None
    c, s = math.cos(angle), math.sin(angle)
    pts = [(origin.x + x * c - y * s, origin.y + x * s + y * c) for x, y in local]
    return np.asarray(pts + pts[:1])


def layer_table(doc: Drawing) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for e in doc.modelspace():
        counts[e.dxf.layer] = counts.get(e.dxf.layer, 0) + 1
    rows = []
    for layer in doc.layers:
        name = layer.dxf.name
        rows.append({
            "name": name,
            "entities": counts.get(name, 0),
            "color": abs(layer.dxf.color),
            "on": layer.is_on(),
            "frozen": layer.is_frozen(),
            "locked": layer.is_locked(),
            "linetype": layer.dxf.linetype,
        })
    return rows


# -- descriptions ------------------------------------------------------------------------
def _pt(v) -> list[float]:
    return [round(float(v[0]), 6), round(float(v[1]), 6)]


def plain(e: DXFGraphic) -> str:
    """Text of a TEXT/ATTRIB/MTEXT without formatting codes. DXF files before R2007 store
    non-ASCII characters as \\U+XXXX escapes, which ezdxf leaves in place."""
    s = e.plain_text() if hasattr(e, "plain_text") else e.dxf.text
    return decode_dxf_unicode(s) if "\\U+" in s else s


def _text_of(e: DXFGraphic) -> str | None:
    t = e.dxftype()
    if t in ("TEXT", "ATTRIB", "ATTDEF", "MTEXT"):
        return plain(e)
    if t == "MULTILEADER" and e.context.mtext is not None:
        return decode_dxf_unicode(e.context.mtext.default_content)
    return None


def _center(e: DXFGraphic) -> list[float]:
    """Centre of a circle/arc in drawing coordinates. Arcs on a tilted plane (3D views, exploded
    proxy graphics) store it in their own coordinate system, and show as ellipses."""
    return _pt(e.ocs().to_wcs(e.dxf.center))


def summary(e: DXFGraphic) -> dict[str, Any]:
    """One-line description of an entity: what a person would read off the Properties panel."""
    t = e.dxftype()
    d: dict[str, Any] = {"handle": e.dxf.handle, "type": t, "layer": e.dxf.layer}
    dx = e.dxf
    try:
        if t == "LINE":
            d.update(start=_pt(dx.start), end=_pt(dx.end), length=round(dx.start.distance(dx.end), 6))
        elif t == "CIRCLE":
            d.update(center=_center(e), radius=dx.radius, diameter=2 * dx.radius)
        elif t == "ARC":
            d.update(center=_center(e), radius=dx.radius, start_angle=dx.start_angle, end_angle=dx.end_angle)
        elif t == "LWPOLYLINE":
            d.update(vertices=len(e), closed=e.closed)
            if dx.const_width:
                d["width"] = dx.const_width
        elif t == "INSERT":
            d.update(block=dx.name, insert=_pt(dx.insert), rotation=dx.rotation,
                     scale=[dx.xscale, dx.yscale])
            atts = {a.dxf.tag: a.dxf.text for a in e.attribs}
            if atts:
                d["attributes"] = atts
        elif t in ("DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"):
            d["text"] = dx.text or "<>"
            try:  # ezdxf cannot measure every dimension type (e.g. large radial / jogged)
                if t == "ARC_DIMENSION":  # ezdxf gives the angle; the dimension shows the arc length
                    raise ValueError
                m = e.get_measurement()
                d["measurement"] = round(float(m), 6) if isinstance(m, (int, float)) else _pt(m)
            except Exception:  # noqa: BLE001
                if dx.hasattr("actual_measurement"):
                    d["measurement"] = round(dx.actual_measurement, 6)
        elif t == "HATCH":
            d.update(pattern=dx.pattern_name, solid=bool(dx.solid_fill), boundaries=len(e.paths))
        elif t == "POINT":
            d.update(location=_pt(dx.location))
        elif t == TABLE:
            rows = table_rows(e)
            # writers often leave the row/column counts at 0; count the cells drawn instead
            d.update(insert=_pt(dx.insert), rows=dx.n_rows or len(rows),
                     cols=dx.n_cols or max((len(r) for r in rows), default=0))
            if rows:
                d["title"] = " | ".join(rows[0])
        txt = _text_of(e)
        if txt is not None:
            d["text"] = txt
            if dx.hasattr("insert"):
                d["insert"] = _pt(dx.insert)
            for key in ("height", "char_height"):
                if dx.is_supported(key) and dx.hasattr(key):
                    d["height"] = dx.get(key)
                    break
    except Exception as ex:  # noqa: BLE001 - descriptions are best effort
        d["note"] = f"partial: {ex}"
    return d


def detail(doc: Drawing, e: DXFGraphic) -> dict[str, Any]:
    d = summary(e)
    attribs = e.dxf.all_existing_dxf_attribs()
    d["dxf"] = {k: (_pt(v) if hasattr(v, "x") else v) for k, v in attribs.items()
                if k not in ("handle", "owner")}
    layer = doc.layers.get(e.dxf.layer) if doc.layers.has_entry(e.dxf.layer) else None
    color = e.dxf.color
    d["color"] = "ByLayer" if color == 256 else "ByBlock" if color == 0 else color
    if layer is not None and color == 256:
        d["effective_color"] = abs(layer.dxf.color)
    bb = entity_bbox(e)
    if bb:
        d["bbox"] = [list(map(lambda v: round(v, 6), bb[0])), list(map(lambda v: round(v, 6), bb[1]))]
    m = measure_entity(e)
    if m:
        d.update(m)
    if e.dxftype() == TABLE:
        d["cells"] = [" | ".join(r) for r in table_rows(e)]
    if e.dxftype() == "INSERT":
        block = doc.blocks.get(e.dxf.name)
        if block is not None:
            kinds: dict[str, int] = {}
            for be in block:
                kinds[be.dxftype()] = kinds.get(be.dxftype(), 0) + 1
            d["block_content"] = kinds
    if e.xdata:
        d["xdata_apps"] = list(e.xdata.data.keys())
    return d


# -- geometry ----------------------------------------------------------------------------
def polylines(e: DXFGraphic, depth: int = 0) -> list[np.ndarray]:
    """World-space polylines (N x 2 arrays) that trace an entity, for picking and highlighting.
    Text-like entities return their bounding box outline."""
    t = e.dxftype()
    if t in INFINITE:
        return []
    if t in ("INSERT", "DIMENSION", "MULTILEADER", "LEADER", TABLE) and depth < 4:
        out: list[np.ndarray] = []
        try:
            for ve in e.virtual_entities():
                out.extend(polylines(ve, depth + 1))
        except Exception:  # noqa: BLE001
            pass
        if t == "INSERT":
            for a in e.attribs:
                out.extend(polylines(a, depth + 1))
        if out:
            return out
    if t not in TEXTUAL:
        try:
            p = ezpath.make_path(e)
            pts = [(v.x, v.y) for v in p.flattening(distance=_flat_tol(e))]
            if len(pts) >= 2:
                return [np.asarray(pts)]
            if len(pts) == 1:
                return [np.asarray(pts * 2)]
        except (TypeError, ValueError, ezdxf.DXFError):
            pass
        except Exception:  # noqa: BLE001
            pass
        if t == "HATCH" or t == "MPOLYGON":
            try:
                return [np.asarray([(v.x, v.y) for v in p.flattening(_flat_tol(e))])
                        for p in ezpath.from_hatch(e) if len(p)]
            except Exception:  # noqa: BLE001
                pass
    if t in TEXTUAL:
        box = text_box(e)
        if box is not None:
            return [box]
    bb = entity_bbox(e)
    if bb is None:
        return []
    (x0, y0), (x1, y1) = bb
    return [np.asarray([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])]


def _flat_tol(e: DXFGraphic) -> float:
    bb = entity_bbox(e)
    if not bb:
        return 0.01
    size = max(bb[1][0] - bb[0][0], bb[1][1] - bb[0][1], 1e-9)
    return max(size / 500.0, 1e-9)


def _seg_distance(p: Point, pts: np.ndarray) -> float:
    if len(pts) == 1:
        return float(np.hypot(*(pts[0] - p)))
    a, b = pts[:-1], pts[1:]
    ab = b - a
    ap = np.asarray(p) - a
    denom = (ab ** 2).sum(axis=1)
    t = np.clip(np.where(denom > 0, (ap * ab).sum(axis=1) / np.where(denom > 0, denom, 1), 0), 0, 1)
    closest = a + ab * t[:, None]
    return float(np.hypot(*(closest - p).T).min())


def _inside(p: Point, pts: np.ndarray) -> bool:
    x, y = p
    xs, ys = pts[:, 0], pts[:, 1]
    xj, yj = np.roll(xs, 1), np.roll(ys, 1)
    cond = ((ys > y) != (yj > y)) & (x < (xj - xs) * (y - ys) / np.where(yj - ys == 0, 1e-30, yj - ys) + xs)
    return bool(cond.sum() % 2)


def distance(e: DXFGraphic, p: Point) -> float:
    """Distance from a point to what is drawn. Filled shapes and text count as solid."""
    t = e.dxftype()
    if t == TABLE:  # a click anywhere on a table, cell interiors included, hits it
        bb = entity_bbox(e)
        if bb and bb[0][0] <= p[0] <= bb[1][0] and bb[0][1] <= p[1] <= bb[1][1]:
            return 0.0
    shapes = polylines(e)
    if not shapes:
        return math.inf
    filled = t in TEXTUAL or t in ("SOLID", "TRACE", "WIPEOUT") or (t == "HATCH" and e.dxf.solid_fill) \
        or (t == "LWPOLYLINE" and e.dxf.const_width > 0)
    best = min(_seg_distance(p, s) for s in shapes)
    if filled and any(_inside(p, s) for s in shapes if len(s) >= 3):
        return 0.0
    if t == "LWPOLYLINE" and e.dxf.const_width > 0:
        best = max(0.0, best - e.dxf.const_width / 2)
    return best


def measure_entity(e: DXFGraphic) -> dict[str, float]:
    t = e.dxftype()
    out: dict[str, float] = {}
    try:
        if t == "CIRCLE":
            r = e.dxf.radius
            return {"length": round(2 * math.pi * r, 6), "area": round(math.pi * r * r, 6)}
        if t in ("LINE", "ARC", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
            p = ezpath.make_path(e)
            pts = np.asarray([(v.x, v.y) for v in p.flattening(distance=_flat_tol(e) / 10)])
            if len(pts) >= 2:
                out["length"] = round(float(np.hypot(*np.diff(pts, axis=0).T).sum()), 6)
            closed = (t == "LWPOLYLINE" and e.closed) or (t == "POLYLINE" and e.is_closed) \
                or (t == "ELLIPSE" and abs(e.dxf.end_param - e.dxf.start_param) >= 2 * math.pi - 1e-9)
            if closed and len(pts) >= 3:
                out["area"] = round(polygon_area(pts), 6)
        if t == "HATCH":
            area = 0.0
            for p in ezpath.from_hatch(e):
                pts = np.asarray([(v.x, v.y) for v in p.flattening(_flat_tol(e) / 10)])
                if len(pts) >= 3:
                    area += polygon_area(pts)
            out["boundary_area_sum"] = round(area, 6)
    except Exception:  # noqa: BLE001
        pass
    return out


def polygon_area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def entity_by_handle(doc: Drawing, handle: str) -> DXFGraphic | None:
    e = doc.entitydb.get(handle.upper())
    return e if (isinstance(e, DXFGraphic) or (e is not None and e.dxftype() == TABLE)) and e.is_alive else None


# CADCraft's display names (as in entity_counts) and common aliases -> DXF types
_TYPE_ALIASES = {
    "2dsolid": {"SOLID"}, "3dface": {"3DFACE"}, "3dpolyline": {"POLYLINE"}, "3dsolid": {"3DSOLID"},
    "attributedefinition": {"ATTDEF"}, "attribute": {"ATTRIB"}, "blockreference": {"INSERT"},
    "block": {"INSERT"}, "multileader": {"MULTILEADER"}, "mleader": {"MULTILEADER"},
    "polyline": {"LWPOLYLINE", "POLYLINE"}, "proxy": {"ACAD_PROXY_ENTITY"}, "table": {TABLE},
    "dimension": {"DIMENSION", "ARC_DIMENSION"},
}


def type_matches(e, name: str) -> bool:
    """Whether an entity is of the type a person or CADCraft calls `name` (any case, spaces and
    underscores ignored): "Block Reference", "INSERT", "Proxy", "ACAD_PROXY_ENTITY"..."""
    key = name.lower().replace(" ", "").replace("_", "")
    t = e.dxftype()
    return key == t.lower().replace("_", "") or t in _TYPE_ALIASES.get(key, ())


def table_rows(e) -> list[list[str]]:
    """Cell texts of an ACAD_TABLE, top row first, read from its drawn MTEXT cells (empty cells
    are not drawn, so short rows have gaps closed up)."""
    try:
        cells = [v for v in e.virtual_entities() if v.dxftype() == "MTEXT"]
    except Exception:  # noqa: BLE001
        return []
    cells.sort(key=lambda m: (-m.dxf.insert.y, m.dxf.insert.x))
    rows: list[list[str]] = []
    row_y = None
    for m in cells:
        y, tol = m.dxf.insert.y, m.dxf.get("char_height", 1.0) * 0.5
        if row_y is None or abs(y - row_y) > tol:
            rows.append([])
            row_y = y
        rows[-1].append(plain(m).replace("\n", " "))
    return rows


def space(doc: Drawing, layout: str = "Model"):
    """Model space, or the paper-space layout of that name."""
    return doc.modelspace() if layout.lower() == "model" else doc.layouts.get(layout)


def texts(doc: Drawing, layout: str = "Model") -> Iterable[DXFGraphic]:
    sp = space(doc, layout)
    yield from sp.query("TEXT MTEXT")
    for ins in sp.query("INSERT"):
        yield from ins.attribs
