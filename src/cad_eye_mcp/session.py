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
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from ezdxf.document import Drawing as DxfDoc
from PIL import Image

from . import deep, diff, gerber, paths, proxy
from .cadcraft import CadCraft, CadCraftError
from .frame import MINIMAP_W, Overlay, compose
from .frame import _fonts as frame_fonts
from .view import RULER_LEFT, RULER_TOP, BBox, Point, View, fit_view, normalize_box

MAX_HIGHLIGHT = 400
FORBIDDEN_COMMANDS = ("save", "qsave", "saveas", "wblock", "export", "exportpdf", "plot", "publish",
                      "share", "close", "closeall", "open", "new", "xattach", "attach", "import",
                      "xopen", "bsave", "bsaveas", "appload", "lisp", "script", "drawingrecovery",
                      "recover", "compareexport", "tableexport", "plottermanager", "-plottermanager",
                      "options", "stylesmanager", "layout.saveas", "pdfbook", "refclose", "aliasedit",
                      "reinit")
# commands that write a file only when given one of these parameters (with any value, or this value)
WRITES_WITH = {"layout": ("option", "saveas"), "dataextraction": ("file", None), "eattext": ("file", None)}
AUTOSAVE_VARS = ("SAVETIME", "SAVEFILEPATH")  # switched off on open, and kept off


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
class Comparison:
    """The open drawing compared with another version of it (the baseline)."""
    baseline: Path
    doc: DxfDoc                    # the baseline as ezdxf reads it
    tolerance: float
    changes: list[diff.Change]
    regions: list[diff.Region]
    unchanged: int
    shapes: dict[int, list] = field(default_factory=dict)  # change index -> overlay polylines


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
    text_index: dict[str, list[tuple]] = field(default_factory=dict)  # space -> text boxes
    layout: str = "Model"          # the space on screen: "Model" or a paper-space layout name
    model_units: str = "unitless"
    paper_units: dict[str, str] = field(default_factory=dict)  # layout name -> mm | in

    @property
    def layouts(self) -> list[str]:
        return list(self.paper_units)
    proxy_origin: dict[str, list[str]] | None = None  # drawn entity -> [proxy/solid handle, class]
    unsupported: dict[str, int] = field(default_factory=dict)  # objects in the file not drawn, by type
    stand_ins: dict[str, list[str]] = field(default_factory=dict)  # original handle -> drawn handles
    comparison: Comparison | None = None
    through: dict[str, str] = field(default_factory=dict)  # model handle -> viewport it was picked in
    # what the screen cannot show (see Session.check)
    not_drawn: list[dict] = field(default_factory=list)    # each entity not drawn: handle, where, reason
    stand_in_summary: dict[str, dict] = field(default_factory=dict)  # by_type, how
    non_visual: dict[str, int] = field(default_factory=dict)          # e.g. lights: never drawn
    engine_skipped: int = 0                                # objects the engine could not read at all
    missing_xrefs: list[str] = field(default_factory=list)


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
        self._aliases: dict[str, str] | None = None  # command name or alias -> command id

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
                work, gsum = self._gerber_dxf(src)
                open_path = work
            else:
                open_path = src
                work = src if kind == "dxf" else cache / f"dwg-{key}.dxf"
                if kind == "dxf" and not deep.has_handles(src):
                    # each engine would number the entities its own way: ezdxf reads CADCraft's copy
                    work = cache / f"dxf-{key}.dxf"

            opened = self._engine_open(open_path)
            notes += opened.get("warnings", []) if isinstance(opened, dict) else []
            # objects the engine could not read from the file at all (none of them is shown)
            skipped = (self.cc.call("inspect_drawing", {}).json().get("skipped") or {}).get("count", 0)
            if skipped:
                notes.append(f"{skipped} objects in the file could not be read by the engine and are not shown; "
                             "check() tells which, when the file is a DXF")
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
                    self._engine_open(open_path)
                    kinds = ", ".join(f"{t} x{n}" for t, n in psum["drawn"].items())
                    how = ", ".join(sorted(psum["how"]))
                    notes.append(f"Drawn by stand-ins ({how}): {kinds}. CADCraft cannot draw these types; "
                                 "clicking a stand-in reports the original under drawn_for")
                if psum["classes"]:
                    notes.append("Custom object classes: " + ", ".join(f"{c} x{n}" for c, n in psum["classes"].items()))
                if psum["not_drawn"]:
                    notes.append("Not drawn (no geometry, proxy graphic or flat-faced ACIS data to show): "
                                 + ", ".join(f"{t} x{n}" for t, n in psum["not_drawn"].items())
                                 + ". check() lists each one with the reason")
            missing_xrefs = self._relink_xrefs(src) if kind != "gerber" else []
            if missing_xrefs:
                notes.append("External references not found, so their content is not shown: "
                             + "; ".join(missing_xrefs[:20]) + ". Put those files next to the drawing to see them.")

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
            d.engine_skipped, d.missing_xrefs = skipped, missing_xrefs
            if psum:
                d.unsupported = dict(psum["not_drawn"])
                d.not_drawn = psum.get("skipped", [])
                d.non_visual = psum.get("non_visual", {})
                if psum["drawn"]:
                    d.stand_in_summary = {"by_type": dict(psum["drawn"]), "how": dict(psum["how"])}
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

    def _engine_open(self, path: Path) -> dict:
        """Open a file in the engine with autosave off: changes made through cad_command must
        never reach a file, not even an autosave copy."""
        opened = self.cc.call("open", {"path": str(path)}).json() or {}
        self.cc.execute("setvar", {"name": "SAVETIME", "value": 0})
        return opened if isinstance(opened, dict) else {}

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
        self._engine_open(d.engine_path)
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
        drawing (DWG and proxy conversions are opened from the cache folder). Returns the ones
        still missing."""
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
        return missing

    @staticmethod
    def _gerber_dxf(src: Path) -> tuple[Path, dict]:
        """The layered DXF made from a Gerber/drill file, folder or zip, and its summary (cached)."""
        cache = paths.cache_dir()
        stem = f"gerber{gerber.VERSION}-{_cache_key(src)}"
        out, meta = cache / f"{stem}.dxf", cache / f"{stem}.json"
        if out.exists() and meta.exists():
            return out, json.loads(meta.read_text(encoding="utf-8"))
        with contextlib.redirect_stdout(sys.stderr):
            summary = gerber.convert(src, out)
        meta.write_text(json.dumps(summary), encoding="utf-8")
        return out, summary

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
        d.history, d.selection, d.thumb = [], [], None
        d.through = {}
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
            if d.comparison is not None:
                overlay.dim = True
                overlay.diff, overlay.regions = self._diff_overlay()
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
                        vp = deep.entity_by_handle(doc, d.through[h]) if h in d.through else None
                        if vp is not None:  # a model-space entity seen through a viewport
                            m, box = deep.model_to_paper(vp), deep.viewport_box(vp)
                            shapes = [part for s in shapes for part in deep.clip(deep.transform(s, m), box)]
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
        missing = sum(d.unsupported.values()) + d.engine_skipped + len(d.missing_xrefs)
        if missing:
            line2 = (f"layers {on}/{len(d.layers)} visible  |  {sel}  |  "
                     f"! {missing} {'object' if missing == 1 else 'objects'} not shown: run check")
        if d.comparison is not None:
            n = Counter(ch.kind for ch in d.comparison.changes)
            line2 = (f"compare vs {d.comparison.baseline.name}: +{n['added']} added (green)  "
                     f"-{n['removed']} removed (red)  ~{n['changed']} changed (blue)  |  {sel}")
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
        if d.comparison is not None:
            state["comparing_with"] = d.comparison.baseline.name
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
            d.through = {h: v for h, v in d.through.items() if h in d.selection}
            vp = deep.viewport_at(doc, d.layout, p) if doc is not None and d.layout != "Model" else None
            if vp is not None:
                ranked += self._pick_through(doc, vp, p, tol)
            # prefer what is actually under the cursor; among equals, the smaller object
            ranked.sort(key=lambda t: (round(t[1] / max(tol, 1e-12), 1), self._size(doc, t[0])))
            self.annotate(markers=[(p, "click")], boxes=[box])
            return ranked

    def _pick_through(self, doc, vp, p: Point, tol: float) -> list[tuple[str, float]]:
        """Model-space entities under paper point `p` inside viewport `vp`, with their distance
        in paper units. Layers frozen in that viewport are skipped."""
        d = self.drawing
        scale = vp.get_scale() or 1.0  # paper units per model unit
        (mx, my), = deep.transform(np.asarray([p]), np.linalg.inv(deep.model_to_paper(vp)))
        mtol = tol / scale
        self.cc.execute("layout.set", {"name": "Model"})
        try:
            handles = self.candidates(((mx - mtol, my - mtol), (mx + mtol, my + mtol)), limit=300)
        finally:
            self.cc.execute("layout.set", {"name": d.layout})
        handles += [h for h in self._texts_near((mx, my), mtol, "Model") if h not in handles]
        out = []
        for h in handles:
            e = deep.entity_by_handle(doc, h)
            if e is None or vp.is_frozen(e.dxf.layer):
                continue
            dist = deep.distance(e, (mx, my))
            if dist <= mtol * 1.5:
                out.append((h, dist * scale))
                d.through[h] = vp.dxf.handle
        return out

    def through_viewport(self, handle: str, p: Point) -> dict[str, Any] | None:
        """For an entity picked through a viewport: which viewport, its scale, the model point."""
        d = self.drawing
        vp = deep.entity_by_handle(self.doc(), d.through[handle]) if handle in d.through else None
        if vp is None:
            return None
        (mx, my), = deep.transform(np.asarray([p]), np.linalg.inv(deep.model_to_paper(vp)))
        return {"viewport": vp.dxf.handle, "paper_units_per_model_unit": round(vp.get_scale(), 6),
                "model_point": [round(mx, 6), round(my, 6)], "space": "Model"}

    def to_model(self, pts: list[Point]) -> tuple[Any, list[Point]] | None:
        """Paper points that all lie in one viewport, mapped to model space: (viewport, points)."""
        d = self.drawing
        doc = self.doc()
        if doc is None or d.layout == "Model" or not pts:
            return None
        vp = deep.viewport_at(doc, d.layout, pts[0])
        if vp is None or any(deep.viewport_at(doc, d.layout, q) is not vp for q in pts[1:]):
            return None
        model = deep.transform(np.asarray(pts), np.linalg.inv(deep.model_to_paper(vp)))
        return vp, [(float(x), float(y)) for x, y in model]

    def _texts_near(self, p: Point, tol: float, space: str | None = None) -> list[str]:
        d = self.drawing
        space = space or d.layout
        if space not in d.text_index:
            doc = self.doc()
            rows = []
            if doc is not None:
                # tables too: a click inside a cell touches none of the table's lines
                tables = (e for e in deep.space(doc, space) if e.dxftype() == deep.TABLE)
                for e in [*deep.texts(doc, space), *tables]:
                    bb = deep.entity_bbox(e)
                    if bb:
                        rows.append((e.dxf.handle, e.dxf.layer, bb[0][0], bb[0][1], bb[1][0], bb[1][1]))
            d.text_index[space] = rows
        visible = self._visible_layers()
        x, y = p
        return [h for h, layer, x0, y0, x1, y1 in d.text_index[space]
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
        through = self.drawing.through
        for h in self._with_stand_ins(handles[:2000]):
            e = deep.entity_by_handle(doc, h) if doc is not None else None
            bb = deep.entity_bbox(e) if e is not None else None
            vp = deep.entity_by_handle(doc, through[h]) if bb and h in through else None
            if vp is not None:  # picked through a viewport: frame it where the sheet shows it
                corners = deep.transform(np.asarray([bb[0], (bb[1][0], bb[0][1]), bb[1], (bb[0][0], bb[1][1])]),
                                         deep.model_to_paper(vp))
                bb = (tuple(corners.min(axis=0)), tuple(corners.max(axis=0)))
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

    # -- completeness ----------------------------------------------------------------
    def check(self) -> tuple[dict[str, Any], list[tuple[Point, str, BBox | None]]]:
        """What the screen does not show of the file, and why: entities not drawn (with the
        reason), entities the engine dropped, hidden layers, other layouts, missing referenced
        files, structural problems of the file, and for PCBs what the conversion left out.
        Returns the report and marks (point, label, box) for the problems located in the
        current space."""
        with self._lock:
            d = self.drawing
            doc = self.doc()
            ours = set(d.proxy_origin or {})  # stand-ins: added by the viewer, not in the file
            findings: list[str] = []
            marks: list[tuple[Point, str, BBox | None]] = []
            report: dict[str, Any] = {}

            entities: list[tuple[str, Any]] = []
            per_space: dict[str, int] = {}
            if doc is not None:
                for space, sp in diff.spaces(doc):
                    es = [e for e in sp if e.dxf.handle not in ours]
                    per_space[space] = len(es)
                    entities += [(space, e) for e in es]
            report["entities_in_file"] = per_space

            # 1. not drawn: the engine cannot draw them and nothing could stand in
            groups: dict[tuple, dict] = {}
            for r in d.not_drawn:
                g = groups.setdefault((r["type"], r["reason"]), {"type": r["type"], "count": 0, "reason": r["reason"],
                                                                 "where": Counter(), "handles": []})
                g["count"] += 1
                g["where"][r["where"]] += 1
                if len(g["handles"]) < 20:
                    g["handles"].append(r["handle"])
                if r["where"] == d.layout and r.get("box"):
                    (x0, y0), (x1, y1) = r["box"]
                    marks.append((((x0 + x1) / 2, (y0 + y1) / 2), f"not drawn: {r['type']}", ((x0, y0), (x1, y1))))
            n_not = sum(d.unsupported.values())
            if n_not:
                report["not_drawn"] = [{**g, "where": dict(g["where"])} for g in groups.values()]
                findings.append("Not drawn: " + ", ".join(f"{t} x{n}" for t, n in d.unsupported.items())
                                + ". Reasons and handles are under not_drawn; inspect works on them.")
            if d.non_visual:
                report["not_drawing_content"] = d.non_visual
                findings.append(", ".join(f"{t} x{n}" for t, n in d.non_visual.items())
                                + ": lights for 3D renderings, not drawn (AutoCAD does not plot them either).")
            if d.stand_in_summary:
                report["drawn_by_stand_ins"] = d.stand_in_summary
                findings.append("Drawn by stand-ins (" + ", ".join(d.stand_in_summary["how"]) + "): "
                                + ", ".join(f"{t} x{n}" for t, n in d.stand_in_summary["by_type"].items())
                                + ". They may look simpler than in the application that made them.")

            # 2. dropped: in the file, but the engine could not read them
            dropped = self._dropped(entities) if d.kind == "dxf" and doc is not None else []
            if dropped:
                report["dropped_by_engine"] = [{k: v for k, v in r.items() if k != "box"} for r in dropped[:50]]
                kinds = Counter(r["type"] for r in dropped)
                findings.append("In the file but not read by the engine, so not shown: "
                                + ", ".join(f"{t} x{n}" for t, n in kinds.items()) + " (see dropped_by_engine).")
                marks += [(((r["box"][0][0] + r["box"][1][0]) / 2, (r["box"][0][1] + r["box"][1][1]) / 2),
                           f"not shown: {r['type']}", r["box"]) for r in dropped if r["space"] == d.layout and r["box"]]
            elif d.engine_skipped:
                report["dropped_by_engine"] = {"count": d.engine_skipped}
                findings.append(f"Not read by the engine, so not shown: {d.engine_skipped} objects"
                                + (" (a DWG is read by the engine itself, so which ones cannot be told)."
                                   if d.kind == "dwg" else "."))

            # 3. referenced files
            refs: dict[str, Any] = {}
            if d.missing_xrefs:
                refs["xrefs_not_found"] = d.missing_xrefs
                findings.append("External references not found, so their content is missing: "
                                + "; ".join(d.missing_xrefs[:5]))
            for space, e in entities:
                t = e.dxftype()
                if t not in ("IMAGE", "PDFUNDERLAY", "DWFUNDERLAY", "DGNUNDERLAY"):
                    continue
                try:
                    definition = e.image_def if t == "IMAGE" else e.get_underlay_def()
                    name = definition.dxf.filename if definition is not None else ""
                except Exception:  # noqa: BLE001
                    name = ""
                if not self._find_file(name):
                    refs.setdefault("files_not_found", []).append({"handle": e.dxf.handle, "type": t, "file": name})
                    bb = deep.entity_bbox(e) if space == d.layout else None
                    if bb:
                        marks.append((((bb[0][0] + bb[1][0]) / 2, (bb[0][1] + bb[1][1]) / 2), f"missing file: {t}", bb))
            if refs.get("files_not_found"):
                findings.append("Images or underlays whose files are missing: "
                                + ", ".join(sorted({r['file'] or '?' for r in refs['files_not_found']})[:5]))
            if refs:
                report["references"] = refs

            # 4. on purpose out of sight: hidden layers, invisible entities, other layouts
            state = {l["name"]: l for l in d.layers}
            off, frozen, invisible = Counter(), Counter(), 0
            for space, e in entities:
                l = state.get(e.dxf.layer)
                if l is not None and l.get("frozen"):
                    frozen[e.dxf.layer] += 1
                elif l is not None and not l.get("on", True):
                    off[e.dxf.layer] += 1
                invisible += bool(e.dxf.get("invisible", 0))
            hidden: dict[str, Any] = {}
            if off or frozen:
                hidden.update({k: dict(v) for k, v in (("layers_off", off), ("layers_frozen", frozen)) if v})
                findings.append("On hidden layers: " + ", ".join(f"{n} x{c}" for n, c in (off + frozen).most_common(8))
                                + ". Show them with layers(action='show', names=[...]).")
            if invisible:
                hidden["invisible_entities"] = invisible
                findings.append(f"Entities flagged invisible in the file (not drawn, as in AutoCAD): {invisible}.")
            model_layers = Counter(e.dxf.layer for space, e in entities if space == "Model")
            vp_frozen = {}
            for name in d.layouts:
                sp = deep.space(doc, name) if doc is not None else None
                for vp in (sp.query("VIEWPORT") if sp is not None else []):
                    names = [n for n in vp.frozen_layers if model_layers.get(n)] if vp.dxf.get("id", 0) != 1 else []
                    if names:
                        vp_frozen[f"{name} / viewport {vp.dxf.handle}"] = names
            if vp_frozen:
                hidden["frozen_in_viewports"] = vp_frozen
                findings.append("Viewports that hide some model-space layers: "
                                + ", ".join(vp_frozen) + " (see frozen_in_viewports).")
            if hidden:
                report["hidden"] = hidden
            undrawn = {r["handle"] for r in d.not_drawn} | {r["handle"] for r in dropped}
            other = {s: sum(1 for sp, e in entities if sp == s and e.dxftype() != "VIEWPORT"
                            and e.dxf.handle not in undrawn)
                     for s in ["Model"] + d.layouts if s != d.layout}
            other = {s: n for s, n in other.items() if n}
            if other:
                report["other_spaces"] = other
                findings.append("Not on this screen: " + ", ".join(f"layout '{s}' with {n} drawable entities"
                                                                   if s != "Model" else
                                                                   f"model space with {n} drawable entities"
                                                                   for s, n in other.items())
                                + ". Switch with layout(name=...).")

            # 5. the file itself
            structure: dict[str, Any] = {}
            notes = self._load_notes()
            if notes:
                structure["read"] = notes
                findings.append("The DXF data: " + "; ".join(notes))
            if d.kind != "gerber":
                try:
                    audit = self.cc.execute("audit", {"fix": False}) or {}
                except CadCraftError:
                    audit = {}
                issues = [i for i in audit.get("issues", []) if (i.get("handle") or "").upper() not in ours]
                if issues:
                    structure["audit_issues"] = len(issues)
                    structure["issues"] = [{k: i.get(k) for k in ("handle", "kind", "location", "message")}
                                           for i in issues[:30]]
                    kinds = Counter(i.get("message") or i.get("kind") for i in issues)
                    findings.append("Problems in the file found by audit (nothing was changed): "
                                    + "; ".join(f"{m} x{n}" for m, n in kinds.most_common(5)))
            if structure:
                report["file_structure"] = structure

            # 6. PCB conversion
            pcb_missing = 0
            if d.gerber_summary:
                gs = d.gerber_summary
                pcb: dict[str, Any] = {}
                skipped = {l["layer"]: l["skipped_objects"] for l in gs["layers"] if l.get("skipped_objects")}
                clear = {l["layer"]: l["clear_primitives"] for l in gs["layers"] if l.get("clear_primitives")}
                if skipped:
                    pcb_missing = sum(skipped.values())
                    pcb["objects_not_converted"] = skipped
                    findings.append("Gerber objects that could not be converted, so not shown: "
                                    + ", ".join(f"{k} x{v}" for k, v in skipped.items()))
                if gs.get("unused_files"):
                    pcb["files_not_used"] = gs["unused_files"]
                    findings.append("Files of the set not recognised as a PCB layer, so not shown: "
                                    + ", ".join(gs["unused_files"][:8]))
                if clear:
                    pcb["clear_polarity_primitives"] = clear
                    findings.append("Clear (negative) polarity is drawn in a near-background colour on "
                                    + ", ".join(f"{k}.clear" for k in clear) + ", not cut out of the copper.")
                if pcb:
                    report["pcb"] = pcb

            incomplete = bool(n_not or dropped or d.engine_skipped or d.missing_xrefs
                              or refs.get("files_not_found") or pcb_missing)
            if incomplete:
                verdict = ("incomplete: part of the file is not on screen. Before concluding that something is "
                           "absent from the drawing, read the findings.")
            elif d.stand_in_summary:
                verdict = "complete, with stand-ins for objects the engine cannot draw itself"
            else:
                verdict = "complete: every entity of the file can be shown"
            return {"verdict": verdict, "findings": findings or ["nothing to report"], **report}, marks[:60]

    def _dropped(self, entities: list[tuple[str, Any]]) -> list[dict]:
        """Entities of the DXF that the engine's session does not have (it could not read them)."""
        d = self.drawing
        listed: dict[str, set[str]] = {}
        try:
            for space in ["Model"] + d.layouts:
                self.cc.execute("layout.set", {"name": space})
                info = self.cc.call("inspect_drawing", {"entities": True, "limit": 1_000_000}).json() or {}
                rows = info.get("entities") or []
                if len(rows) < sum((info.get("counts") or {}).values()):
                    return []  # the engine did not list everything: no basis for a comparison
                listed[space] = {str(r.get("handle", "")).upper() for r in rows}
        except CadCraftError:
            return []
        finally:
            self.cc.execute("layout.set", {"name": d.layout})
        out = []
        for space, e in entities:
            if space not in listed or e.dxf.handle in listed[space]:
                continue
            if e.dxftype() == "VIEWPORT" and e.dxf.get("id", 0) == 1:
                continue  # the sheet's own viewport
            try:
                bb = deep.entity_bbox(e)
            except Exception:  # noqa: BLE001
                bb = None
            row = {"handle": e.dxf.handle, "type": e.dxftype(), "layer": e.dxf.layer, "space": space, "box": bb}
            if type(e).__name__.upper() != e.dxftype().replace("_", ""):
                row["kind"] = type(e).__name__  # e.g. a POLYLINE that is a Polyface mesh
            out.append(row)
        return out

    def _find_file(self, name: str) -> bool:
        """Whether a file a drawing refers to exists: as written, next to the drawing, or by name
        next to the drawing."""
        if not name:
            return False
        p = Path(name)
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        src = self.drawing.source.parent
        return any(c.is_file() for c in (p, src / name, src / base))

    def _load_notes(self) -> list[str]:
        d = self.drawing
        if d.dxf is None:
            return []
        try:
            return list(d.dxf.result(timeout=300)[1])
        except Exception as e:  # noqa: BLE001
            return [f"ezdxf could not read the file ({type(e).__name__}: {e}); exact entity data is unavailable"]

    # -- comparing versions ----------------------------------------------------------
    def compare(self, baseline: str, tolerance: float | None = None) -> Comparison:
        """Compare the open drawing with another version of it (`baseline`, usually the earlier
        revision). Changes are drawn over the dimmed drawing until clear_comparison()."""
        with self._lock:
            d = self.drawing
            src = Path(baseline).expanduser()
            if not src.is_absolute():
                src = Path.cwd() / src
            if not src.exists():
                raise ViewerError(f"File not found: {src}")
            doc = self.doc()
            if doc is None:
                raise ViewerError("The open drawing could not be read as DXF data, so it cannot be compared.")
            with contextlib.redirect_stdout(sys.stderr):
                old_doc, _ = deep.load(self._baseline_dxf(src))
                if d.proxy_origin:
                    # compare the DXF as it was before stand-ins were added (same entities and
                    # handles), so both versions come from the same writer
                    doc, _ = deep.load(self._baseline_dxf(d.source))
            if tolerance is None:
                (x0, y0), (x1, y1) = d.extents
                tolerance = max(math.hypot(x1 - x0, y1 - y0) * 1e-7, 1e-9)
            elif tolerance <= 0:
                raise ViewerError("tolerance must be positive")
            changes, unchanged = diff.compare(doc, old_doc, tolerance)
            regions = diff.regions(changes)
            d.comparison = Comparison(src, old_doc, tolerance, changes, regions, unchanged)
            d.selection = []
            self.annotate()
            return d.comparison

    def _baseline_dxf(self, src: Path) -> Path:
        """The DXF to read for a baseline, made the way open() makes it, so both versions go
        through the same conversion (and share the cache with opening that file)."""
        cache = paths.cache_dir()
        key = _cache_key(src)
        suffix = src.suffix.lower()
        if suffix == ".dwg" or (suffix == ".dxf" and not deep.has_handles(src)):
            out = cache / f"{'dwg' if suffix == '.dwg' else 'dxf'}-{key}.dxf"
            if not out.exists():
                cc = CadCraft()  # a second engine, so the open drawing stays as it is
                try:
                    cc.call("open", {"path": str(src)})
                    cc.call("save", {"path": str(out)})
                finally:
                    cc.close()
            return out
        if suffix == ".dxf" or not gerber.looks_like_gerber(src):
            return src
        return self._gerber_dxf(src)[0]

    def clear_comparison(self) -> None:
        with self._lock:
            self.drawing.comparison = None

    def _comparison(self) -> Comparison:
        c = self.drawing.comparison
        if c is None:
            raise ViewerError("No comparison is active. Call compare(baseline=...) first.")
        return c

    def comparison_summary(self) -> dict[str, Any]:
        c = self._comparison()
        kinds = Counter(ch.kind for ch in c.changes)
        by_type: dict[str, Counter] = defaultdict(Counter)
        by_layer: dict[str, Counter] = defaultdict(Counter)
        blocks: Counter = Counter()
        for ch in c.changes:
            by_type[ch.type][ch.kind] += 1
            by_layer[ch.layer][ch.kind] += 1
            if "block_content" in ch.what.get("changed", {}):
                e = deep.entity_by_handle(self.doc(), ch.handle) if ch.handle else None
                blocks[e.dxf.name if e is not None else "?"] += 1
        out: dict[str, Any] = {
            "baseline": str(c.baseline),
            "added": kinds["added"], "removed": kinds["removed"], "changed": kinds["changed"],
            "unchanged": c.unchanged, "tolerance": float(f"{c.tolerance:.3g}"),
        }
        if not c.changes:
            out["note"] = "No differences: every entity matches the baseline within the tolerance."
            return out
        out["by_type"] = {t: dict(n) for t, n in sorted(by_type.items(), key=lambda kv: -sum(kv[1].values()))[:20]}
        out["by_layer"] = {l: dict(n) for l, n in sorted(by_layer.items(), key=lambda kv: -sum(kv[1].values()))[:20]}
        if blocks:
            out["block_definitions_changed"] = {name: f"{n} references" for name, n in blocks.items()}
        out["regions"] = [self._region_row(r) for r in c.regions[:60]]
        if len(c.regions) > 60:
            out["regions_not_listed"] = len(c.regions) - 60
        unplaced = sum(1 for ch in c.changes if ch.bbox is None)
        if unplaced:
            out["without_position"] = unplaced
        return out

    def _region_row(self, r: diff.Region) -> dict[str, Any]:
        kinds = Counter(self.drawing.comparison.changes[i].kind for i in r.members)
        row: dict[str, Any] = {"region": r.n}
        if r.space != "Model":
            row["layout"] = r.space
        row["box"] = [[round(v, 6) for v in r.box[0]], [round(v, 6) for v in r.box[1]]]
        row.update({k: kinds[k] for k in ("added", "removed", "changed") if kinds[k]})
        return row

    def goto_region(self, n: int) -> dict[str, Any]:
        """Zoom to a numbered region of the comparison (switching layout if needed) and list
        what changed there."""
        with self._lock:
            c = self._comparison()
            region = next((r for r in c.regions if r.n == n), None)
            if region is None:
                raise ViewerError(f"No region {n}; regions are 1..{len(c.regions)}.")
            if region.space != self.drawing.layout:
                self.set_layout(region.space)
            (x0, y0), (x1, y1) = region.box
            pad = max(x1 - x0, y1 - y0) * 0.25 or self.drawing.view.scale * 60
            self._set_view(self.drawing.view.fit(((x0 - pad, y0 - pad), (x1 + pad, y1 + pad)), margin=0))
            rows = [self.change_row(i) for i in sorted(region.members, key=lambda i: (c.changes[i].kind,
                                                                                    c.changes[i].type))]
            out = {**self._region_row(region), "changes": rows[:80]}
            if len(rows) > 80:
                out["changes_not_listed"] = len(rows) - 80
            return out

    def change_row(self, i: int) -> dict[str, Any]:
        """One change as JSON: what it is, its handles, and what differs."""
        c = self._comparison()
        ch = c.changes[i]
        row: dict[str, Any] = {"kind": ch.kind, "type": ch.type, "layer": ch.layer}
        if ch.handle:
            row["handle"] = ch.handle
        if ch.baseline_handle:
            row["baseline_handle"] = ch.baseline_handle
        if ch.kind == "changed":
            row.update(ch.what)
        else:
            doc = self.doc() if ch.kind == "added" else c.doc
            e = deep.entity_by_handle(doc, ch.handle or ch.baseline_handle) if doc is not None else None
            if e is not None:
                row.update({k: v for k, v in deep.summary(e).items() if k not in ("handle", "type", "layer")})
        return row

    def changes_at(self, p: Point, tol: float) -> list[dict]:
        """Changes drawn within `tol` of a point in the current space (old or new version)."""
        d = self.drawing
        c = d.comparison
        if c is None:
            return []
        doc = self.doc()
        hits = []
        for i, ch in enumerate(c.changes):
            if ch.space != d.layout or ch.bbox is None:
                continue
            (x0, y0), (x1, y1) = ch.bbox
            if not (x0 - tol <= p[0] <= x1 + tol and y0 - tol <= p[1] <= y1 + tol):
                continue
            dist = math.inf
            for h, src in ((ch.handle, doc), (ch.baseline_handle, c.doc)):
                e = deep.entity_by_handle(src, h) if h and src is not None else None
                if e is not None:
                    dist = min(dist, deep.distance(e, p))
            if dist <= tol * 1.5:
                hits.append((dist, i))
        hits.sort()
        return [{**self.change_row(i), "region": c.changes[i].region} for _, i in hits[:8]]

    def describe_baseline(self, handles: list[str]) -> list[dict]:
        c = self._comparison()
        out = []
        for h in handles:
            e = deep.entity_by_handle(c.doc, h)
            out.append(deep.detail(c.doc, e) if e is not None else {"handle": h, "note": "not in the baseline"})
        return out

    def _diff_overlay(self) -> tuple[list, list]:
        """(kind, polylines) for the changes in view, and the region boxes of this space."""
        d = self.drawing
        c = d.comparison
        doc = self.doc(wait=120)
        (vx0, vy0), (vx1, vy1) = d.view.window
        known = {l["name"] for l in d.layers}
        visible = self._visible_layers()
        out: list = []
        drawn = 0
        for i, ch in enumerate(c.changes):
            if ch.space != d.layout or ch.bbox is None or (ch.layer in known and ch.layer not in visible):
                continue
            (bx0, by0), (bx1, by1) = ch.bbox
            if bx1 < vx0 or bx0 > vx1 or by1 < vy0 or by0 > vy1:
                continue
            if i not in c.shapes:
                shapes = []
                if ch.baseline_handle:
                    e = deep.entity_by_handle(c.doc, ch.baseline_handle)
                    if e is not None:
                        shapes.append(("was" if ch.kind == "changed" else "removed", deep.polylines(e)))
                if ch.handle and doc is not None:
                    lines = []
                    for part in d.stand_ins.get(ch.handle) or [ch.handle]:
                        e = deep.entity_by_handle(doc, part)
                        if e is not None:
                            lines += deep.polylines(e)
                    shapes.append((ch.kind, lines))
                c.shapes[i] = shapes
            out += c.shapes[i]
            drawn += 1
            if drawn >= 5000:
                break
        regions = [(r.box, str(r.n)) for r in c.regions if r.space == d.layout]
        return out, regions

    # -- raw command passthrough -----------------------------------------------------
    def _command_id(self, command: str) -> str:
        """The command id a name or alias stands for. CADCraft's execute takes ids only today;
        resolving aliases keeps the blocklist whole should that change."""
        if self._aliases is None:
            self._aliases = {}
            for c in self.cc.call("list_commands", {}).json() or []:
                cid = c["id"].lower()
                for name in [c["id"], *c.get("aliases", [])]:
                    self._aliases.setdefault(name.lower(), cid)
        name = command.strip().lower()
        return self._aliases.get(name) or self._aliases.get(name.lstrip("-_.")) or name

    def command(self, command: str, params: dict | None) -> Any:
        params = params or {}
        cid = self._command_id(command)
        if {cid, cid.lstrip("-_."), command.lower().lstrip("-_.")} & set(FORBIDDEN_COMMANDS):
            raise ViewerError(f"'{command}' is not allowed: the viewer never writes or opens files.")
        key, value = WRITES_WITH.get(cid.lstrip("-_."), (None, None))
        if key and params.get(key) and (value is None or str(params[key]).lower() == value):
            raise ViewerError(f"'{command}' with {key} would write a file, which the viewer never does.")
        if cid == "setvar" and str(params.get("name", "")).upper() in AUTOSAVE_VARS:
            raise ViewerError("Autosave stays off: the viewer never writes files.")
        if cid in ("layout.set", "ctab"):
            raise ViewerError("Switch layouts with the layout tool, so the view follows.")
        with self._lock:
            result = self.cc.execute(command, params or {})
            self._refresh_layers()
            return result

    def close(self) -> None:
        self.cc.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
