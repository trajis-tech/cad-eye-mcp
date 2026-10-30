"""Entities CADCraft does not draw -> plain entities it does.

CADCraft keeps entity types it cannot draw as opaque "Proxy" records: custom objects of AutoCAD
Mechanical, Civil 3D, Plant 3D (ACAD_PROXY_ENTITY), ACIS solids, meshes, and newer AutoCAD types
such as ARC_DIMENSION. Drawings made of them look empty or incomplete. For each such entity we
add what can stand in for it, in the same layout or block, and remember where it came from:

- its own geometry when ezdxf can produce it (dimension blocks of ARC_DIMENSION and friends);
- otherwise its proxy graphic: the lines, arcs and fills AutoCAD itself shows when the
  application that made the object is not installed;
- for ACIS solids (3DSOLID, BODY, REGION) and MESH: a wireframe of the edges, read from the
  modeler data. Curved ACIS faces need a full modeling kernel; those solids stay undrawn.

Whatever has none of these is not drawn, and listed with the reason and, when it can be worked
out, where it is. Originals are kept, so their data can still be inspected."""

from __future__ import annotations

import mmap
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic, factory
from ezdxf.proxygraphic import ProxyGraphic

VERSION = 6  # bump when the output or summary changes, so cached conversions are redone
MAX_LISTED = 50_000  # entities not drawn that are listed one by one (all are counted)
MARKERS = (b"ACAD_PROXY_ENTITY", b"3DSOLID", b"\nBODY", b"\nREGION", b"\nMESH")
ALWAYS = {"ACAD_PROXY_ENTITY", "3DSOLID", "BODY", "REGION", "MESH"}
SOLIDS = {"3DSOLID", "BODY", "REGION"}
NON_VISUAL = {"LIGHT"}  # light 3D renderings only; AutoCAD does not plot them either


def needs_explode(path: Path) -> bool:
    """Cheap test (no parsing) whether a DXF file may contain proxy entities, solids or meshes."""
    with open(path, "rb") as f:
        if f.seek(0, 2) == 0:
            return False
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            return any(m.find(marker) >= 0 for marker in MARKERS)


def class_name(doc: Drawing, proxy: DXFGraphic) -> str:
    """The application's class name (e.g. ACAMCOMPENTVIEW): group code 91 indexes the CLASSES
    section from 500."""
    try:
        idx = proxy.acdb_proxy_entity.get_first_value(91) - 500
        return list(doc.classes.classes.values())[idx].dxf.name
    except Exception:  # noqa: BLE001
        return "?"


def _proxy_graphic(doc: Drawing, e: DXFGraphic) -> tuple[list[DXFGraphic], bool]:
    """Entities of a proxy graphic, and whether it was damaged. A damaged record ends the
    graphic; what came before it is kept."""
    parts: list[DXFGraphic] = []
    data = getattr(e, "proxy_graphic", None)
    if not data:
        return parts, False
    try:
        for ve in ProxyGraphic(data, doc=doc).unsafe_virtual_entities():
            parts.append(ve)
    except Exception:  # noqa: BLE001
        return parts, True
    return parts, False


def _edges(faces_of) -> set[tuple]:
    edges: set[tuple] = set()
    for verts, faces in faces_of:
        verts = [tuple(v) for v in verts]
        for face in faces:
            face = list(face)
            for a, b in zip(face, face[1:] + face[:1]):
                if verts[a] != verts[b]:
                    edges.add((min(verts[a], verts[b]), max(verts[a], verts[b])))
    return edges


def _line(e: DXFGraphic, a, b) -> DXFGraphic:
    return factory.new("LINE", {"layer": e.dxf.layer, "color": e.dxf.color, "start": a, "end": b})


def _acis_points(bodies) -> list[tuple]:
    """Vertex positions of ACIS bodies (also of curved ones, which cannot be meshed): enough to
    say where a solid is."""
    pts = []
    for body in bodies:
        lump = body.lump
        while not lump.is_none:
            shell = lump.shell
            while not shell.is_none:
                face = shell.face
                while not face.is_none:
                    loop = face.loop
                    while not loop.is_none:
                        first = co = loop.coedge
                        while not co.is_none:
                            if not co.edge.start_vertex.is_none:
                                pts.append(tuple(co.edge.start_vertex.point.location))
                            co = co.next_coedge
                            if co is first:
                                break
                        loop = loop.next_loop
                    face = face.next_face
                shell = shell.next_shell
            lump = lump.next_lump
    return pts


def _box(pts) -> list[list[float]] | None:
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [[min(xs), min(ys)], [max(xs), max(ys)]]


def _solid(e: DXFGraphic) -> tuple[list[DXFGraphic], str, list | None]:
    """Wireframe of an ACIS solid or MESH, or why there is none (and where it is, if known)."""
    if e.dxftype() == "MESH":
        try:
            data = e.data()
        except Exception:  # noqa: BLE001
            return [], "mesh data unreadable", None
        if not data.faces:
            return [], "mesh without faces", _box(data.vertices)
        return [_line(e, a, b) for a, b in _edges([(data.vertices, data.faces)])], "", None
    from ezdxf.acis import api as acis

    try:
        bodies = acis.load_dxf(e)
    except Exception:  # noqa: BLE001 - encrypted, damaged, or a version ezdxf cannot parse
        return [], "ACIS data unreadable (encrypted, damaged or in a format ezdxf cannot parse)", None
    try:
        meshes = [m for body in bodies for m in acis.mesh_from_body(body)]
    except Exception:  # noqa: BLE001 - curved faces
        return [], "solid with curved faces: drawing it needs a 3D modeling kernel", _box(_acis_points(bodies))
    edges = _edges((m.vertices, m.faces) for m in meshes)
    if not edges:
        return [], "solid without faces", None
    return [_line(e, a, b) for a, b in edges], "", None


def _stand_in(doc: Drawing, e: DXFGraphic) -> tuple[list[DXFGraphic], str, str, list | None]:
    """What to draw for `e` and how it was obtained, or why nothing can be drawn and where `e`
    is (a box, when known)."""
    t = e.dxftype()
    if t in SOLIDS or t == "MESH":
        parts, reason, box = _solid(e)
        if parts:
            return parts, "wireframe", "", None
        parts, _ = _proxy_graphic(doc, e)
        if parts:
            return parts, "proxy graphic", "", None
        return [], "", reason, box
    if t != "ACAD_PROXY_ENTITY":
        try:
            parts = list(e.virtual_entities())
        except Exception:  # noqa: BLE001
            parts = []
        if parts:
            return parts, "geometry", "", None
    parts, damaged = _proxy_graphic(doc, e)
    if parts:
        return parts, "proxy graphic", "", None
    if t == "ACAD_PROXY_ENTITY":
        cls = class_name(doc, e)
        if damaged:
            return [], "", f"custom object {cls}: its proxy graphics could not be read", None
        state = "has empty proxy graphics" if getattr(e, "proxy_graphic", None) else "was saved without proxy graphics"
        return [], "", (f"custom object {cls} {state} (typical of 3D-only data): only the application "
                        "that made it can draw it"), None
    return [], "", f"CADCraft cannot draw {t}, and there is no geometry to rebuild it from", None


def explode(src: Path, out: Path, types: set[str] = frozenset()) -> dict:
    """Write `out`: `src` with every entity of `types` (plus proxy entities, solids and meshes)
    replaced by what can stand in for it. Returns a summary: `drawn` and `not_drawn` counts by
    type, the application classes of proxy entities, `skipped`: each entity not drawn with
    where it is (layout or block), the reason and its box when known, and `origin`: new entity
    handle -> [original handle, its type or class]."""
    doc = ezdxf.readfile(src)
    types = set(types) | ALWAYS
    origin: dict[str, list[str]] = {}
    summary: dict = {"drawn": {}, "not_drawn": {}, "how": {}, "classes": {}, "entities": 0, "skipped": [],
                     "non_visual": {}}
    query = " ".join(sorted(types))
    places = [(l.name, l) for l in doc.layouts] + \
             [(f"block {b.name}", b) for b in doc.blocks if not b.block_record.is_any_layout]
    for where, layout in places:
        for e in list(layout.query(query)):
            t = e.dxftype()
            if t in NON_VISUAL:
                summary["non_visual"][t] = summary["non_visual"].get(t, 0) + 1
                continue
            parts, how, reason, box = _stand_in(doc, e)
            if not parts:
                summary["not_drawn"][t] = summary["not_drawn"].get(t, 0) + 1
                if len(summary["skipped"]) < MAX_LISTED:
                    summary["skipped"].append({"handle": e.dxf.handle, "type": t, "layer": e.dxf.layer,
                                               "where": where, "reason": reason, "box": box})
                continue
            label = class_name(doc, e) if t == "ACAD_PROXY_ENTITY" else t
            if t == "ACAD_PROXY_ENTITY":
                summary["classes"][label] = summary["classes"].get(label, 0) + 1
            summary["drawn"][t] = summary["drawn"].get(t, 0) + 1
            summary["how"][how] = summary["how"].get(how, 0) + 1
            for p in parts:
                p.dxf.discard("owner")
                p.dxf.discard("paperspace")
                factory.bind(p, doc)
                layout.add_entity(p)
                origin[p.dxf.handle] = [e.dxf.handle, label]
            summary["entities"] += len(parts)
            # the original stays: CADCraft does not draw it, and its data remains inspectable
    doc.saveas(out)
    summary["origin"] = origin
    return summary
