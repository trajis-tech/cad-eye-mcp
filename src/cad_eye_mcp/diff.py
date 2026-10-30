"""Compare two versions of a drawing: which entities were added, removed or changed.

Entities are matched by content, not by handle, so drawings re-saved or exported by another
program (which renumbers handles) still compare. Every entity gets a signature from its type,
layer, attributes and geometry rounded to a tolerance; equal signatures are unchanged. What is
left is paired up as changed, in this order: by handle (when the two files share their
numbering), by identical shape somewhere else (moved), and by overlapping position (edited in
place). Whatever still has no partner was added or removed."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
from ezdxf import path as ezpath
from ezdxf.document import Drawing

from . import deep
from .view import BBox

# attributes that only number or link records; they say nothing about the drawing's content
_SKIP = {"handle", "owner", "id", "uid", "geometry"}
_DEFAULTS: dict[tuple[str, str], Any] = {}


class P(tuple):
    """A point. Points are shifted when testing whether an entity only moved."""

    __slots__ = ()


class Pts:
    """Rows of points (x, y, then any other per-vertex columns such as widths and bulge), kept
    as one array: big boards have hundreds of thousands of polylines."""

    __slots__ = ("a",)

    def __init__(self, rows, width: int) -> None:
        a = np.asarray(rows, dtype=float)
        self.a = a.reshape(-1, width) if a.size else np.zeros((0, width))

    def shifted(self, off) -> np.ndarray:
        if not (off[0] or off[1]):
            return self.a
        a = self.a.copy()
        a[:, 0] -= off[0]
        a[:, 1] -= off[1]
        return a


def _pt(v) -> P:
    return P(float(c) for c in v)


def _xyz(vertices) -> Pts:
    return Pts([(v[0], v[1], v[2] if len(v) > 2 else 0.0) for v in vertices], 3)


def _default(e, key: str):
    k = (e.dxftype(), key)
    if k not in _DEFAULTS:
        attr = e.DXFATTRIBS.get(key)
        _DEFAULTS[k] = getattr(attr, "default", None) if attr is not None else None
    return _DEFAULTS[k]


def _digest(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else repr(p).encode())
    return h.hexdigest()[:16]


class Signer:
    """Signatures of one document's entities. Block references sign their block's content, so
    an edited block definition shows up as a change of every reference to it."""

    def __init__(self, doc: Drawing, tol: float):
        self.doc, self.q = doc, tol
        self._blocks: dict[str, str] = {}

    # -- raw properties ---------------------------------------------------------------------
    def props(self, e) -> dict[str, Any]:
        """What makes up an entity, with points marked as P. Defaults are left out, so a writer
        that spells out default values compares equal to one that omits them."""
        t = e.dxftype()
        out: dict[str, Any] = {}
        for k, v in e.dxf.all_existing_dxf_attribs().items():
            if k in _SKIP or k.endswith("_handle"):
                continue
            if t == "INSERT" and k == "name":
                continue  # replaced by the block's content below
            if hasattr(v, "x") and hasattr(v, "y"):
                v = _pt(v)
            elif k == "dimtype":
                v &= ~32  # "block used by this dimension only": set or not depending on the writer
            if v == _default(e, k):
                continue
            out[k] = v
        try:
            self._extra(e, t, out)
        except Exception as ex:  # noqa: BLE001 - an entity ezdxf cannot fully decode
            out["undecodable"] = type(ex).__name__
        return out

    def _extra(self, e, t: str, out: dict) -> None:
        if t in ("TEXT", "ATTRIB", "ATTDEF", "MTEXT"):
            text = deep.plain(e)
            raw = e.text if t == "MTEXT" else e.dxf.text
            out["text"] = text
            if raw != text:
                out["text_format"] = raw
        elif t == "LWPOLYLINE":
            out["vertices"] = Pts(e.lwpoints.values, 5)  # x, y, start width, end width, bulge
        elif t == "POLYLINE":
            vs = list(e.vertices)
            out["vertices"] = Pts([(*v.dxf.location, v.dxf.get("bulge", 0.0)) for v in vs], 4)
            out["vertex_flags"] = tuple((v.dxf.flags, *(v.dxf.get(f"vtx{i}", 0) for i in range(4))) for v in vs)
        elif t == "SPLINE":
            out["control_points"] = _xyz(e.control_points)
            out["fit_points"] = _xyz(e.fit_points)
            out["knots"] = tuple(float(k) for k in e.knots)
            out["weights"] = tuple(float(w) for w in e.weights)
        elif t == "INSERT":
            name = e.dxf.name
            if not name.startswith("*"):  # anonymous block names are renumbered on every save
                out["block"] = name
            out["block_content"] = self.block(name)
            out["attributes"] = tuple((a.dxf.tag, deep.plain(a), _pt(a.dxf.insert)) for a in e.attribs)
        elif t in ("DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION"):
            geometry = e.dxf.get("geometry")
            if geometry:  # the drawn dimension: catches style overrides kept outside the attributes
                out["appearance"] = self.block(geometry)
        elif t in ("HATCH", "MPOLYGON"):
            out["boundaries"] = tuple(_xyz(p.control_vertices()) for p in ezpath.from_hatch(e))
        elif t == "LEADER":
            out["vertices"] = _xyz(e.vertices)
        elif t == "MULTILEADER":
            ctx = e.context
            if ctx.mtext is not None:
                out["text"] = deep.decode_dxf_unicode(ctx.mtext.default_content)
                out["text_at"] = _pt(ctx.mtext.insert)
            out["leaders"] = tuple(_xyz([v for line in ld.lines for v in line.vertices]) for ld in ctx.leaders)
        elif t == deep.TABLE:
            out["cells"] = tuple(tuple(r) for r in deep.table_rows(e))
        elif t == "MESH":
            data = e.data()
            out["mesh"] = _digest(np.round(np.asarray(data.vertices, dtype=float) / self.q).tobytes(),
                                  data.faces)
        elif t in ("3DSOLID", "BODY", "REGION", "SURFACE"):
            sab = getattr(e, "sab", None)
            out["acis"] = _digest(sab if sab else "\n".join(getattr(e, "sat", None) or []))
        else:
            graphic = getattr(e, "proxy_graphic", None)
            if graphic:
                out["proxy_graphic"] = _digest(graphic)

    # -- signatures ---------------------------------------------------------------------------
    def quantize(self, v, off=(0.0, 0.0, 0.0)):
        q = self.q
        if isinstance(v, Pts):
            return v.a.shape, np.round(v.shifted(off) / q).astype(np.int64).tobytes()
        if isinstance(v, P):
            return tuple(round((c - o) / q) for c, o in zip(v, off))
        if isinstance(v, (float, np.floating)):
            return round(float(v) / q)
        if isinstance(v, (tuple, list)):
            return tuple(self.quantize(x, off) for x in v)
        return v

    def signature(self, e, props: dict | None = None, off=(0.0, 0.0, 0.0), drop=()) -> tuple:
        props = self.props(e) if props is None else props
        return (e.dxftype(),) + tuple(sorted((k, self.quantize(v, off)) for k, v in props.items()
                                             if k not in drop))

    def block(self, name: str) -> str:
        """Content hash of a block definition (base point and every entity in it)."""
        if name in self._blocks:
            return self._blocks[name]
        self._blocks[name] = "recursive"
        blk = self.doc.blocks.get(name)
        if blk is None:
            self._blocks[name] = "missing"
            return "missing"
        sigs = sorted(repr(self.signature(e)) for e in blk)
        self._blocks[name] = _digest(self.quantize(_pt(blk.block.dxf.base_point)), sigs)
        return self._blocks[name]


@dataclass
class Item:
    handle: str
    type: str
    layer: str
    space: str
    sig: tuple
    entity: Any
    _props: dict | None = None
    _bbox: Any = False

    def props(self, signer: Signer) -> dict:
        if self._props is None:
            self._props = signer.props(self.entity)
        return self._props

    @property
    def bbox(self) -> BBox | None:
        if self._bbox is False:
            try:
                self._bbox = deep.entity_bbox(self.entity)
            except Exception:  # noqa: BLE001
                self._bbox = None
        return self._bbox


@dataclass
class Change:
    kind: str                       # added | removed | changed
    space: str
    type: str
    layer: str
    handle: str | None              # in the open drawing (added, changed)
    baseline_handle: str | None     # in the baseline drawing (removed, changed)
    bbox: BBox | None
    what: dict = field(default_factory=dict)  # changed: {"moved": [dx, dy]} and/or {"changed": {...}}
    region: int = 0


@dataclass
class Region:
    n: int
    space: str
    box: BBox
    members: list[int]


def spaces(doc: Drawing) -> Iterable[tuple[str, Any]]:
    yield "Model", doc.modelspace()
    for layout in doc.layouts:
        if not layout.is_modelspace:
            yield layout.name, layout


def items(doc: Drawing, signer: Signer) -> list[Item]:
    return [Item(e.dxf.handle, e.dxftype(), e.dxf.layer, name, signer.signature(e), e)
            for name, sp in spaces(doc) for e in sp]


def _anchor(it: Item) -> tuple[float, float]:
    bb = it.bbox
    return (bb[0][0], bb[0][1]) if bb else (0.0, 0.0)


def _close(a, b, tol: float, shift=(0.0, 0.0, 0.0)) -> bool:
    if isinstance(a, Pts) and isinstance(b, Pts):
        return a.a.shape == b.a.shape and bool(np.all(np.abs(b.shifted(shift) - a.a) <= tol))
    if isinstance(a, P) and isinstance(b, P):
        return len(a) == len(b) and all(abs(y - x - s) <= tol for x, y, s in zip(a, b, shift))
    if isinstance(a, (float, np.floating)) and isinstance(b, (float, np.floating)):
        return abs(float(a) - float(b)) <= tol
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_close(x, y, tol, shift) for x, y in zip(a, b))
    return a == b


def show(v, limit: int = 6):
    """A property value as compact JSON: points as [x, y], long lists summarised."""
    if isinstance(v, Pts):
        return [[round(float(c), 6) for c in row[:2]] for row in v.a] if len(v.a) <= limit else f"{len(v.a)} points"
    if isinstance(v, P):
        return [round(c, 6) for c in v[:2]]
    if isinstance(v, (float, np.floating)):
        return round(float(v), 6)
    if isinstance(v, (tuple, list)):
        if len(v) > limit:
            return f"{len(v)} items"
        return [show(x, limit) for x in v]
    if isinstance(v, bytes):
        return f"{len(v)} bytes"
    return v


def describe_change(old: dict, new: dict, tol: float, shift=(0.0, 0.0, 0.0)) -> dict:
    """Properties that differ between two versions of an entity: {name: [old, new]}."""
    out = {}
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a is None or b is None or not _close(a, b, tol, shift):
            if k in ("block_content", "appearance"):
                out[k] = "different"
            elif isinstance(a, Pts) and isinstance(b, Pts) and a.a.shape == b.a.shape and len(a.a) > 6:
                bad = np.nonzero(np.any(np.abs(b.shifted(shift) - a.a) > tol, axis=1))[0]
                i = int(bad[0]) if len(bad) else 0
                out[k] = {"vertices_changed": len(bad), "first": i,
                          "old": show(Pts(a.a[i], a.a.shape[1])), "new": show(Pts(b.a[i], b.a.shape[1]))}
            elif isinstance(a, Pts) and isinstance(b, Pts) and len(a.a) != len(b.a):
                out[k] = {"vertex_count": [len(a.a), len(b.a)]}
            elif isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)) and len(a) == len(b) > 6:
                i = next((i for i, (x, y) in enumerate(zip(a, b)) if not _close(x, y, tol, shift)), 0)
                out[k] = {"first_difference": i, "old": show(a[i]), "new": show(b[i])}
            else:
                out[k] = [show(a) if a is not None else "default", show(b) if b is not None else "default"]
    return out


def compare(new_doc: Drawing, old_doc: Drawing, tol: float) -> tuple[list[Change], int]:
    """Changes from `old_doc` (the baseline) to `new_doc`, and the number of unchanged entities."""
    sn, so = Signer(new_doc, tol), Signer(old_doc, tol)
    new_items, old_items = items(new_doc, sn), items(old_doc, so)

    # 1. identical content
    pool: dict[tuple, list[Item]] = defaultdict(list)
    for it in old_items:
        pool[(it.space, it.sig)].append(it)
    left_new, same_handle, unchanged = [], 0, 0
    for it in new_items:
        bucket = pool.get((it.space, it.sig))
        if bucket:
            twin = bucket.pop()
            unchanged += 1
            same_handle += twin.handle == it.handle
        else:
            left_new.append(it)
    left_old = [it for bucket in pool.values() for it in bucket]
    pairs: list[tuple[Item, Item]] = []

    # 2. same handle: only when the files clearly share their numbering (same drawing, re-saved)
    if unchanged >= 5 and same_handle >= 0.5 * unchanged:
        by_handle = {(it.space, it.handle, it.type): it for it in left_old}
        rest = []
        for it in left_new:
            twin = by_handle.pop((it.space, it.handle, it.type), None)
            (pairs.append((twin, it)) if twin is not None else rest.append(it))
        left_new, left_old = rest, list(by_handle.values())

    # 3. the same shape somewhere else (moved, maybe to another layer)
    def shape(it: Item, signer: Signer) -> tuple:
        ax, ay = _anchor(it)
        return it.space, signer.signature(it.entity, it.props(signer), (ax, ay, 0.0), drop=("layer",))

    groups: dict[tuple, list[Item]] = defaultdict(list)
    for it in left_old:
        groups[shape(it, so)].append(it)
    rest = []
    for it in left_new:
        cands = groups.get(shape(it, sn))
        if cands:
            ax, ay = _anchor(it)
            j = min(range(len(cands)), key=lambda i: math.dist(_anchor(cands[i]), (ax, ay)))
            pairs.append((cands.pop(j), it))
        else:
            rest.append(it)
    left_new = rest
    taken = {id(o) for o, _ in pairs}
    left_old = [it for it in left_old if id(it) not in taken]

    # 4. edited in place: same type, overlapping boxes; nearest centre wins, same layer preferred
    left_new, left_old = _pair_in_place(left_new, left_old, pairs, tol)

    changes: list[Change] = []
    for old, new in pairs:
        po, pn = old.props(so), new.props(sn)
        (ox, oy), (nx, ny) = _anchor(old), _anchor(new)
        shift = (nx - ox, ny - oy, 0.0)
        what: dict[str, Any] = {}
        moved_only = describe_change(po, pn, tol * 1.5, shift)
        if (abs(shift[0]) > tol or abs(shift[1]) > tol) and not moved_only:
            what["moved"] = [round(shift[0], 6), round(shift[1], 6)]
        else:
            diff = describe_change(po, pn, tol * 1.5)
            if not diff:
                unchanged += 1  # equal within the tolerance (a rounding boundary split them)
                continue
            if moved_only and len(moved_only) < len(diff):
                what["moved"] = [round(shift[0], 6), round(shift[1], 6)]
                diff = moved_only
            what["changed"] = diff
        changes.append(Change("changed", new.space, new.type, new.layer, new.handle, old.handle,
                              _union(old.bbox, new.bbox), what))
    changes += [Change("added", it.space, it.type, it.layer, it.handle, None, it.bbox) for it in left_new]
    changes += [Change("removed", it.space, it.type, it.layer, None, it.handle, it.bbox) for it in left_old]
    return changes, unchanged


def _pair_in_place(left_new: list[Item], left_old: list[Item], pairs: list, tol: float):
    by_type: dict[tuple, list[Item]] = defaultdict(list)
    for it in left_old:
        if it.bbox:
            by_type[(it.space, it.type)].append(it)
    used: set[int] = set()
    candidates = []
    for it in left_new:
        olds = by_type.get((it.space, it.type))
        bb = it.bbox
        if not olds or not bb:
            continue
        boxes = np.asarray([[o.bbox[0][0], o.bbox[0][1], o.bbox[1][0], o.bbox[1][1]] for o in olds])
        m = max(tol * 10, 1e-9)
        hit = ((boxes[:, 0] <= bb[1][0] + m) & (boxes[:, 2] >= bb[0][0] - m)
               & (boxes[:, 1] <= bb[1][1] + m) & (boxes[:, 3] >= bb[0][1] - m))
        idx = np.nonzero(hit)[0]
        c =((bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2)
        for i in idx:
            o = olds[i]
            oc = ((boxes[i, 0] + boxes[i, 2]) / 2, (boxes[i, 1] + boxes[i, 3]) / 2)
            candidates.append((o.layer != it.layer, math.dist(c, oc), id(it), it, o))
    candidates.sort(key=lambda t: t[:2])
    paired_new: set[int] = set()
    for _, _, nid, new, old in candidates:
        if nid in paired_new or id(old) in used:
            continue
        pairs.append((old, new))
        paired_new.add(nid)
        used.add(id(old))
    rest_new = [it for it in left_new if id(it) not in paired_new]
    rest_old = [it for it in left_old if id(it) not in used]
    return rest_new, rest_old


def _union(a: BBox | None, b: BBox | None) -> BBox | None:
    if a is None:
        return b
    if b is None:
        return a
    return ((min(a[0][0], b[0][0]), min(a[0][1], b[0][1])), (max(a[1][0], b[1][0]), max(a[1][1], b[1][1])))


def regions(changes: list[Change]) -> list[Region]:
    """Group changes that lie close together, per space, numbered in reading order (top to
    bottom, left to right). Changes without a position form no region."""
    out: list[Region] = []
    by_space: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(changes):
        if c.bbox is not None:
            by_space[c.space].append(i)
    for space, idx in by_space.items():
        boxes = np.asarray([[changes[i].bbox[0][0], changes[i].bbox[0][1],
                             changes[i].bbox[1][0], changes[i].bbox[1][1]] for i in idx], dtype=float)
        lo, hi = boxes[:, :2].min(axis=0), boxes[:, 2:].max(axis=0)
        diag = float(np.hypot(*(hi - lo))) or 1.0
        margin = diag * 0.04
        big = ((boxes[:, 2] - boxes[:, 0]) > (hi[0] - lo[0]) * 0.5) & ((boxes[:, 3] - boxes[:, 1]) > (hi[1] - lo[1]) * 0.5) \
            if len(idx) > 1 else np.zeros(len(idx), bool)
        groups = _cluster(boxes, margin, big)
        for members in groups:
            b = boxes[members]
            box = ((float(b[:, 0].min()), float(b[:, 1].min())), (float(b[:, 2].max()), float(b[:, 3].max())))
            out.append(Region(0, space, box, [idx[m] for m in members]))
    out.sort(key=lambda r: (r.space != "Model", r.space, -round(r.box[1][1], 6), r.box[0][0]))
    for n, r in enumerate(out, 1):
        r.n = n
        for i in r.members:
            changes[i].region = n
    return out


def _cluster(boxes: np.ndarray, margin: float, big: np.ndarray) -> list[list[int]]:
    n = len(boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    small = np.nonzero(~big)[0]
    if len(small) <= 4000:
        e = boxes[small] + np.asarray([-margin, -margin, margin, margin])
        for k, i in enumerate(small):
            hit = ((e[k + 1:, 0] <= e[k, 2]) & (e[k + 1:, 2] >= e[k, 0])
                   & (e[k + 1:, 1] <= e[k, 3]) & (e[k + 1:, 3] >= e[k, 1]))
            for j in small[k + 1:][hit]:
                ri, rj = find(int(i)), find(int(j))
                if ri != rj:
                    parent[rj] = ri
    else:  # many changes: neighbouring cells of a coarse grid form a region
        lo = boxes[:, :2].min(axis=0)
        cell = max(margin * 5, 1e-12)
        cells: dict[tuple[int, int], int] = {}
        for i in small:
            cx, cy = (boxes[i, 0] + boxes[i, 2]) / 2, (boxes[i, 1] + boxes[i, 3]) / 2
            key = (int((cx - lo[0]) // cell), int((cy - lo[1]) // cell))
            if key in cells:
                parent[find(int(i))] = find(cells[key])
            else:
                cells[key] = int(i)
        for (gx, gy), i in cells.items():
            for nb in ((gx + 1, gy), (gx, gy + 1), (gx + 1, gy + 1), (gx + 1, gy - 1)):
                j = cells.get(nb)
                if j is not None:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())
