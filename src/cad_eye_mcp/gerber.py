"""Gerber (RS-274X) and Excellon drill files -> a layered DXF that CADCraft can show.

gerbonara parses the files into graphic primitives (in mm); each primitive becomes filled DXF
geometry: wide LWPOLYLINEs with round caps for tracks, filled discs for round pads, SOLIDs for
rectangles and solid HATCHes for regions. Every PCB layer gets its own DXF layer, so the AI can
show, hide and isolate layers like in a PCB viewer."""

from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
from gerbonara import ExcellonFile, GerberFile, LayerStack
from gerbonara import graphic_primitives as gp
from gerbonara.utils import MM

GERBER_EXT = {
    ".gbr", ".ger", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gko", ".gm1",
    ".gml", ".gm", ".gpt", ".gpb", ".pho", ".cmp", ".sol", ".plc", ".pls", ".stc", ".sts", ".crc",
    ".crs", ".art", ".g1", ".g2", ".g3", ".g4", ".gp1", ".gp2", ".gdl", ".gtd", ".gbd", ".gtc",
}
DRILL_EXT = {".drl", ".drd", ".xln", ".exc", ".ncd", ".nc", ".tap"}

# (side, use) -> (DXF layer, ACI colour, visible initially, draw order)
STYLE = {
    ("bottom", "copper"): ("B.Cu", 5, True, 10),
    ("inner", "copper"): ("In.Cu", 3, True, 20),
    ("top", "copper"): ("F.Cu", 1, True, 30),
    ("bottom", "silk"): ("B.SilkS", 211, False, 34),
    ("top", "silk"): ("F.SilkS", 7, True, 40),
    ("bottom", "mask"): ("B.Mask", 92, False, 5),
    ("top", "mask"): ("F.Mask", 211, False, 35),
    ("bottom", "paste"): ("B.Paste", 8, False, 6),
    ("top", "paste"): ("F.Paste", 9, False, 36),
    ("mechanical", "outline"): ("Edge.Cuts", 2, True, 50),
}
DRILL_STYLE = {"pth": ("Drill.PTH", 4, True, 60), "npth": ("Drill.NPTH", 140, True, 61),
               "other": ("Drill", 4, True, 62)}
CLEAR_COLOR = 250  # "clear" (negative) polarity features are drawn in a near-background colour
VERSION = 2  # bump when the output or summary changes, so cached conversions are redone


def looks_like_gerber(path: Path) -> bool:
    if path.is_dir():
        return any(p.suffix.lower() in GERBER_EXT | DRILL_EXT for p in path.iterdir() if p.is_file())
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return any(Path(n).suffix.lower() in GERBER_EXT | DRILL_EXT for n in z.namelist())
    if suffix in GERBER_EXT | DRILL_EXT:
        return True
    try:
        head = path.read_bytes()[:2048].decode("ascii", "ignore")
    except OSError:
        return False
    return head.lstrip().startswith(("%FS", "%MO", "G04", "M48", "%TF")) or "%FSLA" in head


@dataclass
class _Layer:
    name: str
    color: int
    visible: bool
    order: int
    source: str
    files: list[tuple[str, object]] = field(default_factory=list)


# Fallback when gerbonara cannot map a file set: classic extensions (Protel/Altium, Eagle) and
# KiCad-style name fragments.
_BY_EXT = {
    ".gtl": ("top", "copper"), ".cmp": ("top", "copper"), ".gbl": ("bottom", "copper"),
    ".sol": ("bottom", "copper"), ".gto": ("top", "silk"), ".plc": ("top", "silk"),
    ".gbo": ("bottom", "silk"), ".pls": ("bottom", "silk"), ".gts": ("top", "mask"),
    ".stc": ("top", "mask"), ".gbs": ("bottom", "mask"), ".sts": ("bottom", "mask"),
    ".gtp": ("top", "paste"), ".crc": ("top", "paste"), ".gbp": ("bottom", "paste"),
    ".crs": ("bottom", "paste"), ".gko": ("mechanical", "outline"), ".gm1": ("mechanical", "outline"),
    ".gml": ("mechanical", "outline"), ".gm": ("mechanical", "outline"),
}
_BY_NAME = [("f_cu", ("top", "copper")), ("f.cu", ("top", "copper")), ("b_cu", ("bottom", "copper")),
            ("b.cu", ("bottom", "copper")), ("f_silk", ("top", "silk")), ("b_silk", ("bottom", "silk")),
            ("f_mask", ("top", "mask")), ("b_mask", ("bottom", "mask")), ("f_paste", ("top", "paste")),
            ("b_paste", ("bottom", "paste")), ("edge", ("mechanical", "outline")),
            ("outline", ("mechanical", "outline"))]


def _manual_stack(files: list[tuple[str, bytes]]) -> tuple[list[_Layer], None]:
    layers: dict[str, _Layer] = {}
    for fname, data in files:
        low = fname.lower()
        suffix = Path(low).suffix
        text = data.decode("latin-1")
        is_drill = suffix in DRILL_EXT or text.lstrip().startswith("M48")
        try:
            cam = ExcellonFile.from_string(text) if is_drill else GerberFile.from_string(text)
        except Exception:  # noqa: BLE001 - not a CAM file
            continue
        if is_drill:
            name, color, vis, order = DRILL_STYLE["npth" if "npth" in low else "other"]
        else:
            key = _BY_EXT.get(suffix) or next((k for frag, k in _BY_NAME if frag in low), None)
            name, color, vis, order = STYLE.get(key, (Path(fname).stem, 8, True, 70))
        layers.setdefault(name, _Layer(name, color, vis, order, fname)).files.append((fname, cam))
    return sorted(layers.values(), key=lambda l: l.order), None


def _file_names(path: Path) -> list[str]:
    if path.is_dir():
        return sorted(p.name for p in path.iterdir() if p.is_file())
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            return sorted(Path(n).name for n in z.namelist() if not n.endswith("/"))
    return [path.name]


def _read_files(path: Path) -> list[tuple[str, bytes]]:
    if path.is_dir():
        return [(p.name, p.read_bytes()) for p in sorted(path.iterdir())
                if p.is_file() and p.suffix.lower() in GERBER_EXT | DRILL_EXT | {".txt"}]
    with zipfile.ZipFile(path) as z:
        return [(Path(n).name, z.read(n)) for n in z.namelist()
                if Path(n).suffix.lower() in GERBER_EXT | DRILL_EXT | {".txt"}]


def _layers_from(path: Path) -> tuple[list[_Layer], str]:
    if path.is_dir() or path.suffix.lower() == ".zip":
        try:
            stack = LayerStack.open_dir(path) if path.is_dir() else LayerStack.open_zip(path)
        except Exception:  # noqa: BLE001 - gerbonara could not map the file names
            layers, _ = _manual_stack(_read_files(path))
            if not layers:
                raise ValueError(f"no Gerber or drill files found in {path}") from None
            return layers, path.stem
    else:
        stack = None
        try:
            stack = LayerStack.from_files([path])
        except Exception:  # noqa: BLE001 - single unknown file: open it directly below
            pass
        if stack is None or (not stack.graphic_layers and not list(stack.drill_layers)):
            if path.suffix.lower() in DRILL_EXT:
                f = ExcellonFile.open(path)
                return [_Layer(*DRILL_STYLE["other"], path.name, [(path.name, f)])], path.stem
            f = GerberFile.open(path)
            return [_Layer(path.stem, 1, True, 30, path.name, [(path.name, f)])], path.stem

    layers: dict[str, _Layer] = {}
    inner = 0
    for (side, use), f in stack.graphic_layers.items():
        fname = Path(str(getattr(f, "original_path", "") or f"{side} {use}")).name
        if (side, use) in STYLE:
            name, color, vis, order = STYLE[(side, use)]
            if side == "inner":
                inner += 1
                name, color, order = f"In{inner}.Cu", (3, 30, 150, 40, 200, 70)[(inner - 1) % 6], 20 + inner
        else:
            name, color, vis, order = f"{side}.{use}", 8, False, 70
        layers.setdefault(name, _Layer(name, color, vis, order, fname)).files.append((fname, f))
    for key, f in (("pth", stack.drill_pth), ("npth", stack.drill_npth)):
        if f is not None:
            fname = Path(str(getattr(f, "original_path", "") or key)).name
            layers.setdefault(DRILL_STYLE[key][0], _Layer(*DRILL_STYLE[key], fname)).files.append((fname, f))
    for f in getattr(stack, "_drill_layers", []):
        fname = Path(str(getattr(f, "original_path", "") or "drill")).name
        layers.setdefault("Drill", _Layer(*DRILL_STYLE["other"], fname)).files.append((fname, f))
    if getattr(stack, "tmpdir", None) is not None:
        stack.tmpdir.cleanup()  # a zip's files, unpacked by gerbonara; all parsed by now
    return sorted(layers.values(), key=lambda l: l.order), stack.board_name or path.stem


def _sweep(x1, y1, x2, y2, cx, cy, clockwise: bool) -> float:
    a1, a2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
    if math.isclose(x1, x2, abs_tol=1e-9) and math.isclose(y1, y2, abs_tol=1e-9):
        return -2 * math.pi if clockwise else 2 * math.pi
    s = (a2 - a1) % (2 * math.pi)
    return s - 2 * math.pi if clockwise else s


def _bulge_vertices(points_and_arcs) -> list[tuple[float, float, float]]:
    """(p1, p2, (clockwise, center)) segments -> LWPOLYLINE vertices (x, y, bulge).
    Full circles are split in two half arcs because a single bulge cannot exceed 180°."""
    out = []
    for (x1, y1), (x2, y2), (cw, center) in points_and_arcs:
        if cw is None:
            out.append((x1, y1, 0.0))
            continue
        cx, cy = center
        s = _sweep(x1, y1, x2, y2, cx, cy, cw)
        if abs(s) > math.pi:
            a1 = math.atan2(y1 - cy, x1 - cx)
            r = math.hypot(x1 - cx, y1 - cy)
            mx, my = cx + r * math.cos(a1 + s / 2), cy + r * math.sin(a1 + s / 2)
            b = math.tan(s / 8)
            out += [(x1, y1, b), (mx, my, b)]
        else:
            out.append((x1, y1, math.tan(s / 4)))
    return out


class _Writer:
    def __init__(self, doc):
        self.msp = doc.modelspace()
        self.caps: set[tuple[str, int, int, int]] = set()

    def disc(self, layer: str, x: float, y: float, r: float, attribs: dict) -> None:
        if r <= 0:
            self.msp.add_point((x, y), dxfattribs=attribs)
            return
        self.msp.add_lwpolyline([(x - r / 2, y, 1.0), (x + r / 2, y, 1.0)], format="xyb", close=True,
                                dxfattribs={**attribs, "const_width": r})

    def cap(self, layer: str, x: float, y: float, r: float, attribs: dict) -> None:
        key = (layer, round(x * 1e4), round(y * 1e4), round(r * 1e4))
        if key not in self.caps:
            self.caps.add(key)
            self.disc(layer, x, y, r, attribs)

    def primitive(self, layer: str, prim, attribs: dict) -> None:
        if isinstance(prim, gp.Line):
            w = prim.width
            if w <= 1e-6:
                self.msp.add_line((prim.x1, prim.y1), (prim.x2, prim.y2), dxfattribs=attribs)
                return
            if not prim.is_zero_size():
                self.msp.add_lwpolyline([(prim.x1, prim.y1), (prim.x2, prim.y2)],
                                        dxfattribs={**attribs, "const_width": w})
            self.cap(layer, prim.x1, prim.y1, w / 2, attribs)
            self.cap(layer, prim.x2, prim.y2, w / 2, attribs)
        elif isinstance(prim, gp.Arc):
            w = prim.width
            verts = _bulge_vertices([((prim.x1, prim.y1), (prim.x2, prim.y2), (prim.clockwise, (prim.cx, prim.cy)))])
            verts.append((prim.x2, prim.y2, 0.0))
            self.msp.add_lwpolyline(verts, format="xyb", dxfattribs={**attribs, "const_width": max(w, 0.0)})
            if w > 1e-6:
                self.cap(layer, prim.x1, prim.y1, w / 2, attribs)
                self.cap(layer, prim.x2, prim.y2, w / 2, attribs)
        elif isinstance(prim, gp.Circle):
            self.disc(layer, prim.x, prim.y, prim.r, attribs)
        elif isinstance(prim, gp.Rectangle):
            c, s = math.cos(prim.rotation), math.sin(prim.rotation)
            hw, hh = prim.w / 2, prim.h / 2
            pts = [(prim.x + dx * c - dy * s, prim.y + dx * s + dy * c)
                   for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
            # SOLID vertex order is 1-2-4-3 (zig-zag)
            self.msp.add_solid([pts[0], pts[1], pts[3], pts[2]], dxfattribs=attribs)
        elif isinstance(prim, gp.ArcPoly):
            if len(prim.outline) < 2:
                return
            verts = _bulge_vertices(list(prim.segments))
            hatch = self.msp.add_hatch(color=256, dxfattribs=attribs)  # ezdxf defaults to 7
            hatch.paths.add_polyline_path(verts, is_closed=True)


def convert(path: Path, out_dxf: Path) -> dict:
    """Write `out_dxf` from a Gerber/drill file, a folder of them, or a zip. Returns a summary."""
    layers, board = _layers_from(path)
    doc = ezdxf.new("R2010", setup=False)
    doc.header["$INSUNITS"] = 4  # mm
    doc.header["$MEASUREMENT"] = 1
    w = _Writer(doc)
    summary = {"board": board, "units": "mm", "layers": []}
    for layer in layers:
        for name, visible in ((layer.name, layer.visible), (layer.name + ".clear", layer.visible)):
            if name not in doc.layers:
                lyr = doc.layers.add(name, color=layer.color if not name.endswith(".clear") else CLEAR_COLOR)
                if not visible:
                    lyr.off()
        counts = {"dark": 0, "clear": 0, "skipped": 0}
        for fname, cam in layer.files:
            for obj in cam.objects:
                try:
                    prims = list(obj.to_primitives(unit=MM))
                except Exception:  # noqa: BLE001 - objects gerbonara cannot expand are counted
                    counts["skipped"] += 1
                    continue
                for prim in prims:
                    dark = getattr(prim, "polarity_dark", True)
                    lname = layer.name if dark else layer.name + ".clear"
                    w.primitive(lname, prim, {"layer": lname})
                    counts["dark" if dark else "clear"] += 1
        summary["layers"].append({"layer": layer.name, "files": [f for f, _ in layer.files],
                                  "visible": layer.visible, "primitives": counts["dark"],
                                  "clear_primitives": counts["clear"], "skipped_objects": counts["skipped"]})
    used = {f for layer in layers for f, _ in layer.files}
    summary["unused_files"] = [n for n in _file_names(path) if n not in used]
    for name in [l.dxf.name for l in doc.layers if l.dxf.name.endswith(".clear")]:
        if not any(True for _ in doc.modelspace().query(f'*[layer=="{name}"]')):
            doc.layers.remove(name)
    doc.saveas(out_dxf)
    return summary
