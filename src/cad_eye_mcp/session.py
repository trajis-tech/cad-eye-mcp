"""The viewer session: one open drawing, its view, layer state and selection.

Every navigation step re-renders the view through CADCraft and returns a framed image plus a
short text state, so the AI can look, act, and look again the way a person uses a CAD viewer."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ezdxf.document import Drawing as DxfDoc
from PIL import Image

from . import deep, gerber, paths, proxy
from .cadcraft import CadCraft, CadCraftError
from .frame import MINIMAP_W, Overlay, compose
from .frame import _fonts as frame_fonts
from .view import RULER_LEFT, RULER_TOP, BBox, Point, View, fit_view, normalize_box

MAX_HIGHLIGHT = 400
FORBIDDEN_COMMANDS = ("save", "qsave", "saveas", "wblock", "export", "exportpdf", "plot", "publish",
                      "share", "close", "closeall", "open", "new", "xattach", "attach", "import",
                      "xopen", "bsave", "bsaveas", "appload", "lisp", "script", "drawingrecovery",
                      "recover", "compareexport", "tableexport", "plottermanager", "-plottermanager",
                      "options", "stylesmanager")


class ViewerError(RuntimeError):
    pass


def guess_cjk_region(texts: list[str]) -> str | None:
    """Pick the glyph region for the drawing's text: kana -> jp; otherwise compare characters only
    encodable in GB2312 (simplified), Big5 (traditional) or only in Big5-HKSCS (Hong Kong)."""
    kana = simp = trad = hk = 0
    for t in texts:
        for ch in t:
            o = ord(ch)
            if 0x3040 <= o <= 0x30FF:
                kana += 1
                continue
            if o < 0x2E80:
                continue
            in_big5 = _encodes(ch, "big5")
            in_gb = _encodes(ch, "gb2312")
            if in_gb and not in_big5:
                simp += 1
            elif in_big5 and not in_gb:
                trad += 1
            elif not in_big5 and _encodes(ch, "big5hkscs"):
                hk += 1
    if kana >= 3 and kana >= simp + trad + hk:
        return "jp"
    if hk >= 3 and hk * 4 >= trad:
        return "hk"
    if simp > trad and simp >= 3:
        return "sc"
    if trad >= 3:
        return "tc"
    return None


def _encodes(ch: str, codec: str) -> bool:
    try:
        ch.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


@dataclass
class Drawing:
    source: Path
    kind: str                      # dxf | dwg | gerber
    work_dxf: Path | None          # the DXF ezdxf reads (same handles as CADCraft's session)
    engine_path: Path              # what CADCraft opened
    title: str
    units: str
    counts: dict[str, int]
    extents: BBox
    layers: list[dict]
    view: View
    notes: list[str] = field(default_factory=list)
    history: list[View] = field(default_factory=list)
    selection: list[str] = field(default_factory=list)
    markers: list[tuple[Point, str]] = field(default_factory=list)
    boxes: list[BBox] = field(default_factory=list)
    lines: list[list[Point]] = field(default_factory=list)
    dxf: Future | None = None
    gerber_summary: dict | None = None
    thumb: tuple[str, Any, View] | None = None
    text_index: list[tuple] | None = None
    layout: str = "Model"          # the space on screen: "Model" or a paper-space layout name
    model_units: str = "unitless"
    paper_units: dict[str, str] = field(default_factory=dict)  # layout name -> mm | in

    @property
    def layouts(self) -> list[str]:
        return list(self.paper_units)
    proxy_origin: dict[str, list[str]] | None = None  # drawn entity -> [proxy/solid handle, class]
    unsupported: dict[str, int] = field(default_factory=dict)  # objects in the file not drawn, by type
    stand_ins: dict[str, list[str]] = field(default_factory=dict)  # original handle -> drawn handles


def _cache_key(path: Path) -> str:
    h = hashlib.sha1(str(path.resolve()).encode())
    items = sorted(path.rglob("*")) if path.is_dir() else [path]
    for p in items:
        if p.is_file():
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


class Session:
    def __init__(self) -> None:
        self.cc = CadCraft()
        self.d: Drawing | None = None
        self.size = (1200, 760)
        self.grid = True
        self.minimap = True
        self.cjk_auto = "CAD_EYE_CJK" not in os.environ  # an explicit setting is never overridden
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ezdxf")

    # -- helpers ---------------------------------------------------------------------
    @property
    def drawing(self) -> Drawing:
        if self.d is None:
            raise ViewerError("No drawing is open. Call open_drawing first.")
        return self.d

    def doc(self, wait: float = 300) -> DxfDoc | None:
        d = self.drawing
        if d.dxf is None:
            return None
        try:
            return d.dxf.result(timeout=wait)[0]
        except Exception:  # noqa: BLE001 - ezdxf could not read the file; CADCraft still works
            return None

    def _visible_layers(self) -> set[str]:
        return {l["name"] for l in self.drawing.layers if l.get("on", True) and not l.get("frozen", False)}

    def _refresh_layers(self) -> None:
        info = self.cc.call("inspect_drawing", {}).json()
        self.drawing.layers = info.get("layers", [])
        self.drawing.thumb = None

    def _set_view(self, v: View) -> None:
        d = self.drawing
        d.history.append(d.view)
        del d.history[:-50]
        d.view = v

    def point(self, x: float, y: float, coords: str) -> Point:
        return self.drawing.view.to_world(x, y, coords)

    def annotate(self, markers=(), boxes=(), lines=()) -> None:
        """Replace the on-screen annotations (pick box, markers, measurement path)."""
        d = self.drawing
        d.markers, d.boxes, d.lines = list(markers), list(boxes), list(lines)

    # -- open ------------------------------------------------------------------------
    def open(self, path: str) -> Drawing:
        with self._lock:
            src = Path(path).expanduser()
            if not src.is_absolute():
                src = Path.cwd() / src
            if not src.exists():
                raise ViewerError(f"File not found: {src}")
            notes: list[str] = []
            gsum = None
            if src.suffix.lower() == ".dwg":
                kind = "dwg"
            elif src.suffix.lower() == ".dxf":
                kind = "dxf"
            elif gerber.looks_like_gerber(src):
                kind = "gerber"
            else:
                kind = "dxf"

            cache = paths.cache_dir()
            key = _cache_key(src)
            if kind == "gerber":
                work = cache / f"gerber-{key}.dxf"
                meta = cache / f"gerber-{key}.json"
                if not work.exists() or not meta.exists():
                    with contextlib.redirect_stdout(sys.stderr):
                        gsum = gerber.convert(src, work)
                    meta.write_text(json.dumps(gsum), encoding="utf-8")
                else:
                    gsum = json.loads(meta.read_text(encoding="utf-8"))
                open_path = work
            else:
                open_path = src
                work = src if kind == "dxf" else cache / f"dwg-{key}.dxf"
                if kind == "dxf" and not deep.has_handles(src):
                    # each engine would number the entities its own way: ezdxf reads CADCraft's copy
                    work = cache / f"dxf-{key}.dxf"

            opened = self.cc.call("open", {"path": str(open_path)}).json() or {}
            notes += opened.get("warnings", []) if isinstance(opened, dict) else []
            if work != open_path and kind != "gerber" and not work.exists():
                self.cc.call("save", {"path": str(work)})

            psum = None
            undrawn = self._undrawn_types() if kind != "gerber" else set()
            if kind != "gerber" and (undrawn or proxy.needs_explode(work)):
                stem = f"explode{proxy.VERSION}-{key}"
                pwork, pmeta = cache / f"{stem}.dxf", cache / f"{stem}.json"
                if not pwork.exists() or not pmeta.exists():
                    with contextlib.redirect_stdout(sys.stderr):
                        psum = proxy.explode(work, pwork, undrawn)
                    pmeta.write_text(json.dumps(psum), encoding="utf-8")
                else:
                    psum = json.loads(pmeta.read_text(encoding="utf-8"))
                if psum["drawn"]:
                    work = open_path = pwork
                    self.cc.call("open", {"path": str(open_path)})
                    kinds = ", ".join(f"{t} x{n}" for t, n in psum["drawn"].items())
                    how = ", ".join(sorted(psum["how"]))
                    notes.append(f"Drawn by stand-ins ({how}): {kinds}. CADCraft cannot draw these types; "
                                 "clicking a stand-in reports the original under drawn_for")
                if psum["classes"]:
                    notes.append("Custom object classes: " + ", ".join(f"{c} x{n}" for c, n in psum["classes"].items()))
                if psum["not_drawn"]:
                    notes.append("Not drawn (no geometry, proxy graphic or flat-faced ACIS data to show): "
                                 + ", ".join(f"{t} x{n}" for t, n in psum["not_drawn"].items()))
            if kind != "gerber":
                notes += self._relink_xrefs(src)

            info = self.cc.call("inspect_drawing", {}).json()
            counts = info.get("counts", {})
            ext = info.get("extents")
            extents: BBox = ((ext[0][0], ext[0][1]), (ext[1][0], ext[1][1])) if ext else ((0, 0), (100, 100))
            units_code = (self.cc.execute("getvar", {"name": "INSUNITS"}) or {}).get("value", 0)
            units = deep.UNIT_NAMES.get(units_code, "unitless")
            title = src.name

            d = Drawing(src, kind, work, open_path, title, units, counts, extents, info.get("layers", []),
                        fit_view(extents, *self.size), notes=notes, gerber_summary=gsum,
                        model_units=units, paper_units=self._layout_units() if kind != "gerber" else {},
                        proxy_origin=psum["origin"] if psum and psum["origin"] else None)
            if psum:
                d.unsupported = dict(psum["not_drawn"])
                for new, (orig, _) in psum["origin"].items():
                    d.stand_ins.setdefault(orig, []).append(new)
            if work is not None:
                d.dxf = self._pool.submit(self._load_dxf, work)
            if self.d is not None and self.d.dxf is not None:
                self.d.dxf.cancel()
            self.d = d
            if not ext:
                # nothing drawable in model space (e.g. 3D-only objects): show a sheet that has content
                sheet = self._first_drawn_layout()
                if sheet:
                    notes.append(f"Model space has nothing to draw; showing layout '{sheet}'")
                else:
                    notes.append("Nothing in this drawing can be drawn" + (
                        f" (model space holds: {', '.join(f'{k} x{n}' for k, n in counts.items())})" if counts else ""))
            if d.layouts:
                notes.append(f"Paper-space layouts: {', '.join(d.layouts)}. Switch with layout(name=...); "
                             "title blocks, drawing views and notes often live there.")
            if (d.layout == "Model" and any(k in counts for k in ("Xline", "Ray"))
                    and info.get("entityCount", 0) <= 20000):
                doc = self.doc()
                if doc is not None:
                    robust = deep.extents(doc.modelspace())
                    if robust:
                        d.extents = robust
                        d.view = fit_view(robust, *self.size)
                        notes.append("view fitted without infinite XLINE/RAY entities")
            if self.cjk_auto and kind != "gerber":
                found = self.cc.call("find_text", {"limit": 3000}).json() or {}
                region = guess_cjk_region([t.get("text", "") for t in found.get("texts", [])])
                if region and region != self.cjk:
                    self._restart_engine(region)
                    notes.append(f"CJK glyphs set to {region.upper()} from the drawing's text")
            return d

    # -- CJK glyph region --------------------------------------------------------------
    @property
    def cjk(self) -> str:
        return os.environ.get("CAD_EYE_CJK", "tc").lower()

    def set_cjk(self, region: str) -> None:
        """Choose TC/HK/SC/JP glyph forms for CJK text (restarts the engine, keeps the view)."""
        region = region.lower()
        if region not in ("tc", "hk", "sc", "jp"):
            raise ViewerError("cjk must be tc, hk, sc or jp")
        with self._lock:
            self.cjk_auto = False
            if region != self.cjk:
                self._restart_engine(region)

    def _restart_engine(self, region: str) -> None:
        os.environ["CAD_EYE_CJK"] = region
        frame_fonts.clear()  # overlay labels follow the region too
        self.cc.close()
        d = self.d
        if d is None:
            return
        self.cc.call("open", {"path": str(d.engine_path)})
        self._relink_xrefs(d.source)
        if d.layout != "Model":
            self.cc.execute("layout.set", {"name": d.layout})
        for layer in d.layers:
            self.cc.execute("layer.set", {"name": layer["name"], "on": layer.get("on", True),
                                          "frozen": layer.get("frozen", False)})
        d.thumb = None

    # -- external references and layouts -------------------------------------------------
    def _relink_xrefs(self, src: Path) -> list[str]:
        """Point xrefs CADCraft could not find at files of the same name next to the source
        drawing (DWG and proxy conversions are opened from the cache folder). Returns notes."""
        try:
            xrefs = (self.cc.execute("xref") or {}).get("xrefs", [])
        except CadCraftError:
            return []
        siblings = {p.name.lower(): p for p in src.parent.iterdir() if p.is_file()} if src.parent.is_dir() else {}
        missing = []
        for x in xrefs:
            if x.get("status") != "Not Found":
                continue
            ref = (x.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1] or f"{x['name']}.dwg"
            found = siblings.get(ref.lower())
            if found is not None:
                try:
                    self.cc.execute("-xref", {"option": "path", "names": x["name"], "path": str(found)})
                    continue
                except CadCraftError:
                    pass
            missing.append(f"{x['name']} ({x.get('path')})")
        if not missing:
            return []
        return ["External references not found, so their content is not shown: " + "; ".join(missing[:20])
                + ". Put those files next to the drawing to see them."]

    def _layout_units(self) -> dict[str, str]:
        """Paper-space layouts in tab order, each with its paper units (mm or in)."""
        try:
            rows = self.cc.execute("layout.list") or []
        except CadCraftError:
            return {}
        rows = [r for r in rows if isinstance(r, dict) and r.get("name", "").lower() != "model"]
        return {r["name"]: "mm" if (r.get("page") or {}).get("paperMm") else "in"
                for r in sorted(rows, key=lambda r: r.get("tabOrder", 0))}

    def _undrawn_types(self) -> set[str]:
        """DXF types of the entities CADCraft keeps as opaque "Proxy" records (it cannot draw them)."""
        try:
            res = self.cc.call("query_entities", {"type": "Proxy", "limit": 200}).json() or {}
        except CadCraftError:
            return set()
        return {t for e in res.get("entities", []) if (t := (e.get("geometry") or {}).get("dxf_type"))}

    def _first_drawn_layout(self) -> str | None:
        """Switch to the first layout with something drawn besides viewports; stay in model
        space (and return None) when every layout is empty."""
        for name in self.drawing.layouts:
            self.cc.execute("layout.set", {"name": name})
            info = self.cc.call("inspect_drawing", {}).json()
            if info.get("extents") and any(k != "Viewport" for k in info.get("counts", {})):
                self._enter_layout(name)
                return name
        self.cc.execute("layout.set", {"name": "Model"})
        return None

    def set_layout(self, name: str) -> None:
        """Show model space ("Model") or a paper-space layout (sheet with title block and viewports)."""
        with self._lock:
            d = self.drawing
            lookup = {n.lower(): n for n in ["Model"] + d.layouts}
            if name.lower() not in lookup:
                raise ViewerError(f"Unknown layout '{name}'. Layouts: {', '.join(lookup.values())}")
            name = lookup[name.lower()]
            self.cc.execute("layout.set", {"name": name})
            self._enter_layout(name)

    def _enter_layout(self, name: str) -> None:
        d = self.drawing
        info = self.cc.call("inspect_drawing", {}).json()
        ext = info.get("extents")
        extents: BBox | None = ((ext[0][0], ext[0][1]), (ext[1][0], ext[1][1])) if ext else None
        if extents is None:
            doc = self.doc()
            extents = deep.extents(deep.space(doc, name)) if doc is not None else None
        d.layout = name
        d.units = d.model_units if name == "Model" else d.paper_units.get(name, "mm")
        d.counts = info.get("counts", {})
        d.layers = info.get("layers", d.layers)
        d.extents = extents or ((0, 0), (100, 100))
        d.view = fit_view(d.extents, *self.size)
        d.history, d.selection, d.text_index, d.thumb = [], [], None, None
        self.annotate()

    @staticmethod
    def _load_dxf(path: Path):
        with contextlib.redirect_stdout(sys.stderr):
            return deep.load(path)

    # -- frames ----------------------------------------------------------------------
    def frame(self, extra: dict | None = None) -> tuple[bytes, str]:
        with self._lock:
            d = self.drawing
            v = d.view
            png = self.cc.render(v.window, v.width_px, v.height_px)
            overlay = Overlay(markers=list(d.markers), boxes=list(d.boxes), lines=list(d.lines))
            if d.selection:
                doc = self.doc(wait=120)
                if doc is not None:
                    for h in d.selection[:MAX_HIGHLIGHT]:
                        # an object and its stand-ins form one highlight (one ring if small)
                        shapes = []
                        for part in (d.stand_ins.get(h.upper()) or [h])[:MAX_HIGHLIGHT]:
                            e = deep.entity_by_handle(doc, part)
                            if e is not None:
                                shapes += deep.polylines(e)
                        if shapes:
                            overlay.highlights.append(shapes)
            mm = self._minimap() if self.minimap else None
            status = self._status()
            img = compose(v, png, overlay, status, minimap=mm, grid=self.grid)
            return img, self.state_text(extra)

    def _minimap(self):
        d = self.drawing
        (ex0, ey0), (ex1, ey1) = d.extents
        ew, eh = max(ex1 - ex0, 1e-9), max(ey1 - ey0, 1e-9)
        v = d.view
        if v.width >= ew * 0.95 and v.height >= eh * 0.95:
            return None  # the whole drawing is already on screen
        key = json.dumps([[l["name"], l.get("on"), l.get("frozen")] for l in d.layers])
        if d.thumb is None or d.thumb[0] != key:
            th = max(40, min(160, int(MINIMAP_W * eh / ew)))
            tv = fit_view(d.extents, MINIMAP_W, th, margin=0.02)
            img = Image.open(io.BytesIO(self.cc.render(tv.window, MINIMAP_W, th))).convert("RGB")
            d.thumb = (key, img, tv)
        return d.thumb[1], d.thumb[2]

    def _status(self) -> list[str]:
        d = self.drawing
        v = d.view
        (x0, y0), (x1, y1) = v.window
        (ex0, ey0), (ex1, ey1) = d.extents
        zoom = max(ex1 - ex0, ey1 - ey0, 1e-12) / max(v.width, v.height)
        on = sum(1 for l in d.layers if l.get("on", True) and not l.get("frozen"))
        title = d.title if d.layout == "Model" else f"{d.title} [{d.layout}]"
        line1 = (f"{title}  |  view x {x0:.6g}..{x1:.6g}  y {y0:.6g}..{y1:.6g} {d.units}  |  "
                 f"{v.scale:.4g} {d.units}/px  |  zoom {zoom:.3g}x")
        sel = f"selected {len(d.selection)}" + (f" ({', '.join(d.selection[:4])}{'…' if len(d.selection) > 4 else ''})" if d.selection else "")
        line2 = f"layers {on}/{len(d.layers)} visible  |  {sel}  |  px coords = this image's pixels"
        return [line1, line2]

    def state_text(self, extra: dict | None = None) -> str:
        d = self.drawing
        v = d.view
        (x0, y0), (x1, y1) = v.window
        state: dict[str, Any] = {
            "view": {"x": [float(f"{x0:.6g}"), float(f"{x1:.6g}")], "y": [float(f"{y0:.6g}"), float(f"{y1:.6g}")],
                     "units": d.units, "units_per_px": float(f"{v.scale:.4g}")},
            "image_px": {"size": list(v.frame_size), "drawing_area": [RULER_LEFT, RULER_TOP,
                                                                     RULER_LEFT + v.width_px, RULER_TOP + v.height_px]},
            "selection": d.selection[:20],
            "hidden_layers": [l["name"] for l in d.layers if not l.get("on", True) or l.get("frozen")],
        }
        if d.layout != "Model":
            state["layout"] = d.layout
        if extra:
            state.update(extra)
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=str)

    # -- navigation ------------------------------------------------------------------
    def zoom(self, factor: float, at: Point | None) -> None:
        with self._lock:
            self._set_view(self.drawing.view.zoom(factor, at))

    def zoom_window(self, p: Point, q: Point) -> None:
        with self._lock:
            box = normalize_box(p, q)
            if box[1][0] - box[0][0] <= 0 and box[1][1] - box[0][1] <= 0:
                raise ViewerError("The window has no size.")
            self._set_view(self.drawing.view.fit(box, margin=0.0))

    def zoom_extents(self) -> None:
        with self._lock:
            self._set_view(self.drawing.view.fit(self.drawing.extents))

    def zoom_to(self, handles: list[str], margin: float = 0.15) -> BBox:
        with self._lock:
            box = self.bbox_of(handles)
            if box is None:
                raise ViewerError("None of those handles has visible geometry.")
            (x0, y0), (x1, y1) = box
            pad = max(x1 - x0, y1 - y0) * margin or self.drawing.view.scale * 40
            self._set_view(self.drawing.view.fit(((x0 - pad, y0 - pad), (x1 + pad, y1 + pad)), margin=0))
            return box

    def pan(self, dx: float, dy: float) -> None:
        with self._lock:
            self._set_view(self.drawing.view.pan(dx, dy))

    def back(self) -> bool:
        with self._lock:
            d = self.drawing
            if not d.history:
                return False
            d.view = d.history.pop()
            return True

    def resize(self, width_px: int, height_px: int) -> None:
        with self._lock:
            width_px = max(320, min(int(width_px), 1800))
            height_px = max(240, min(int(height_px), 1400))
            self.size = (width_px, height_px)
            if self.d is not None:
                self.d.view = self.d.view.resized(width_px, height_px)
                self.d.thumb = None

    # -- layers ----------------------------------------------------------------------
    def layer_rows(self) -> list[dict]:
        d = self.drawing
        rows = [dict(l) for l in d.layers]
        doc = self.doc(wait=0.05) if d.dxf is not None and d.dxf.done() else None
        if doc is not None:
            counts: dict[str, int] = {}
            for e in deep.space(doc, d.layout):
                counts[e.dxf.layer] = counts.get(e.dxf.layer, 0) + 1
            for r in rows:
                r["entities"] = counts.get(r["name"], 0)
        return rows

    def set_layers(self, action: str, names: list[str]) -> list[str]:
        with self._lock:
            d = self.drawing
            all_names = [l["name"] for l in d.layers]
            lookup = {n.lower(): n for n in all_names}
            missing = [n for n in names if n.lower() not in lookup]
            if missing:
                raise ViewerError(f"Unknown layer(s): {', '.join(missing)}. Layers: {', '.join(all_names)}")
            chosen = {lookup[n.lower()] for n in names}
            changes: dict[str, dict] = {}
            if action == "show":
                changes = {n: {"on": True, "frozen": False} for n in chosen}
            elif action == "hide":
                changes = {n: {"on": False} for n in chosen}
            elif action == "isolate":
                changes = {n: ({"on": True, "frozen": False} if n in chosen else {"on": False}) for n in all_names}
            elif action == "show_all":
                changes = {n: {"on": True, "frozen": False} for n in all_names}
            else:
                raise ViewerError("action must be list, show, hide, isolate or show_all")
            for name, params in changes.items():
                self.cc.execute("layer.set", {"name": name, **params})
            self._refresh_layers()
            return sorted(changes)

    # -- picking & selection ---------------------------------------------------------
    def candidates(self, box: BBox | None, crossing: bool = True, limit: int = 2000,
                   type_: str | None = None, layer: str | None = None) -> list[str]:
        """Handles CADCraft finds on visible layers, inside `box` (None: anywhere, which also
        finds entities without drawable geometry)."""
        args: dict[str, Any] = {"crossing": crossing, "limit": limit}
        if box is not None:
            args["window"] = [list(box[0]), list(box[1])]
        if type_:
            args["type"] = type_
        if layer:
            args["layer"] = layer
        res = self.cc.call("query_entities", args).json() or {}
        visible = self._visible_layers()
        return [e["handle"] for e in res.get("entities", []) if e.get("layer") in visible]

    def pick(self, p: Point, tolerance_px: float = 6) -> list[tuple[str, float]]:
        with self._lock:
            d = self.drawing
            tol = tolerance_px * d.view.scale
            box = ((p[0] - tol, p[1] - tol), (p[0] + tol, p[1] + tol))
            handles = self.candidates(box, limit=300)
            doc = self.doc()
            # CADCraft under-estimates how far CJK text runs; add texts from our own index
            handles += [h for h in self._texts_near(p, tol) if h not in handles]
            ranked: list[tuple[str, float]] = []
            for h in handles:
                e = deep.entity_by_handle(doc, h) if doc is not None else None
                dist = deep.distance(e, p) if e is not None else math.inf
                # CADCraft's window test is generous (e.g. block extents); keep only what is
                # really drawn near the point. Entities ezdxf cannot see (inf) stay as fallbacks.
                if math.isinf(dist) or dist <= tol * 1.5:
                    ranked.append((h, dist))
            # prefer what is actually under the cursor; among equals, the smaller object
            ranked.sort(key=lambda t: (round(t[1] / max(tol, 1e-12), 1), self._size(doc, t[0])))
            self.annotate(markers=[(p, "click")], boxes=[box])
            return ranked

    def _texts_near(self, p: Point, tol: float) -> list[str]:
        d = self.drawing
        if d.text_index is None:
            doc = self.doc()
            rows = []
            if doc is not None:
                # tables too: a click inside a cell touches none of the table's lines
                tables = (e for e in deep.space(doc, d.layout) if e.dxftype() == deep.TABLE)
                for e in [*deep.texts(doc, d.layout), *tables]:
                    bb = deep.entity_bbox(e)
                    if bb:
                        rows.append((e.dxf.handle, e.dxf.layer, bb[0][0], bb[0][1], bb[1][0], bb[1][1]))
            d.text_index = rows
        visible = self._visible_layers()
        x, y = p
        return [h for h, layer, x0, y0, x1, y1 in d.text_index
                if layer in visible and x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol]

    def find_type(self, type_: str | None, layer: str | None, box: BBox | None, limit: int) -> list[str]:
        """Entities by type and/or layer on visible layers (inside `box` if given). CADCraft
        answers first; ezdxf fills in types CADCraft does not filter on (Proxy, ACAD_TABLE...)."""
        d = self.drawing
        hs = self.candidates(box, type_=type_, layer=layer, limit=max(limit, 1))
        doc = self.doc() if not hs and type_ else None
        if doc is None:
            return hs
        visible = self._visible_layers()
        for e in deep.space(doc, d.layout):
            if not deep.type_matches(e, type_) or e.dxf.layer not in visible:
                continue
            if layer and e.dxf.layer.lower() != layer.lower():
                continue
            if box is not None:
                bb = deep.entity_bbox(e)
                if bb is None or bb[1][0] < box[0][0] or bb[0][0] > box[1][0] or bb[1][1] < box[0][1] \
                        or bb[0][1] > box[1][1]:
                    continue
            hs.append(e.dxf.handle)
            if len(hs) >= limit:
                break
        return hs

    def table_hits(self, text: str, layer: str | None = None) -> list[dict]:
        """Tables with rows containing `text` (CADCraft's text search does not look in tables)."""
        doc = self.doc()
        if doc is None:
            return []
        needle = text.lower()
        out = []
        for e in deep.space(doc, self.drawing.layout):
            if e.dxftype() != deep.TABLE or (layer and e.dxf.layer.lower() != layer.lower()):
                continue
            rows = [" | ".join(r) for r in deep.table_rows(e)]
            hits = [r for r in rows if needle in r.lower()]
            if hits:
                out.append({"handle": e.dxf.handle, "type": deep.TABLE, "layer": e.dxf.layer, "rows": hits[:10]})
        return out

    def _size(self, doc, handle: str) -> float:
        e = deep.entity_by_handle(doc, handle) if doc is not None else None
        bb = deep.entity_bbox(e) if e is not None else None
        if not bb:
            return math.inf
        return (bb[1][0] - bb[0][0]) * (bb[1][1] - bb[0][1])

    def _with_stand_ins(self, handles: list[str]) -> list[str]:
        """Handles plus the entities drawn in place of those CADCraft cannot draw."""
        stand_ins = self.drawing.stand_ins
        out = []
        for h in handles:
            out.append(h)
            out += stand_ins.get(h.upper(), [])
        return out

    def bbox_of(self, handles: list[str]) -> BBox | None:
        doc = self.doc()
        boxes = []
        for h in self._with_stand_ins(handles[:2000]):
            e = deep.entity_by_handle(doc, h) if doc is not None else None
            bb = deep.entity_bbox(e) if e is not None else None
            if bb:
                boxes.append(bb)
        if not boxes:
            return None
        return ((min(b[0][0] for b in boxes), min(b[0][1] for b in boxes)),
                (max(b[1][0] for b in boxes), max(b[1][1] for b in boxes)))

    def describe(self, handles: list[str], full: bool = False) -> list[dict]:
        doc = self.doc()
        out = []
        for h in handles:
            e = deep.entity_by_handle(doc, h) if doc is not None else None
            if e is None:
                out.append({"handle": h, "note": "not in the DXF data (CADCraft-only entity)"})
                continue
            row = deep.detail(doc, e) if full else deep.summary(e)
            src = (self.drawing.proxy_origin or {}).get(e.dxf.handle)
            if src:
                # drawn in place of a proxy entity or ACIS solid the viewer cannot show directly
                row["drawn_for"] = {"handle": src[0], "object": src[1]}
            out.append(row)
        return out

    # -- raw command passthrough -----------------------------------------------------
    def command(self, command: str, params: dict | None) -> Any:
        if command.lower().lstrip("-_.") in FORBIDDEN_COMMANDS or command.lower() in FORBIDDEN_COMMANDS:
            raise ViewerError(f"'{command}' is not allowed: the viewer never writes or opens files.")
        if command.lower() in ("layout.set", "ctab"):
            raise ViewerError("Switch layouts with the layout tool, so the view follows.")
        with self._lock:
            result = self.cc.execute(command, params or {})
            self._refresh_layers()
            return result

    def close(self) -> None:
        self.cc.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
