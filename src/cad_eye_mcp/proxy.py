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

Whatever has none of these is counted as not drawn. Originals are kept, so their data can still
be inspected."""

from __future__ import annotations

import mmap
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic, factory
from ezdxf.proxygraphic import ProxyGraphic

VERSION = 3  # bump when the output changes, so cached conversions are redone
MARKERS = (b"ACAD_PROXY_ENTITY", b"3DSOLID", b"\nBODY", b"\nREGION", b"\nMESH")
ALWAYS = {"ACAD_PROXY_ENTITY", "3DSOLID", "BODY", "REGION", "MESH"}
SOLIDS = {"3DSOLID", "BODY", "REGION"}


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


def _proxy_graphic(doc: Drawing, e: DXFGraphic) -> list[DXFGraphic]:
    """Entities of a proxy graphic. A damaged record ends the graphic; keep what came before it."""
    parts: list[DXFGraphic] = []
    data = getattr(e, "proxy_graphic", None)
    if not data:
        return parts
    try:
        for ve in ProxyGraphic(data, doc=doc).unsafe_virtual_entities():
            parts.append(ve)
    except Exception:  # noqa: BLE001
        pass
    return parts


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


def _wireframe(e: DXFGraphic) -> list[DXFGraphic]:
    """Edges of an ACIS solid made of flat faces, or of a MESH, as LINEs with its layer and colour."""
    if e.dxftype() == "MESH":
        data = e.data()
        edges = _edges([(data.vertices, data.faces)])
    else:
        from ezdxf.acis import api as acis

        edges = _edges((m.vertices, m.faces) for body in acis.load_dxf(e) for m in acis.mesh_from_body(body))
    attribs = {"layer": e.dxf.layer, "color": e.dxf.color}
    return [factory.new("LINE", dict(attribs, start=a, end=b)) for a, b in edges]


def _stand_in(doc: Drawing, e: DXFGraphic) -> tuple[list[DXFGraphic], str]:
    """What to draw for `e`, and how it was obtained."""
    t = e.dxftype()
    if t in SOLIDS or t == "MESH":
        try:
            parts = _wireframe(e)
        except Exception:  # noqa: BLE001 - curved faces, encrypted or unknown ACIS versions
            parts = []
        if parts:
            return parts, "wireframe"
    elif t != "ACAD_PROXY_ENTITY":
        try:
            parts = list(e.virtual_entities())
        except Exception:  # noqa: BLE001
            parts = []
        if parts:
            return parts, "geometry"
    parts = _proxy_graphic(doc, e)
    return parts, "proxy graphic" if parts else ""


def explode(src: Path, out: Path, types: set[str] = frozenset()) -> dict:
    """Write `out`: `src` with every entity of `types` (plus proxy entities, solids and meshes)
    replaced by what can stand in for it. Returns a summary: `drawn` and `not_drawn` counts by
    type, the application classes of proxy entities, and `origin`: new entity handle ->
    [original handle, its type or class]."""
    doc = ezdxf.readfile(src)
    types = set(types) | ALWAYS
    origin: dict[str, list[str]] = {}
    summary: dict = {"drawn": {}, "not_drawn": {}, "how": {}, "classes": {}, "entities": 0}
    query = " ".join(sorted(types))
    for layout in list(doc.layouts) + [b for b in doc.blocks if not b.block_record.is_any_layout]:
        for e in list(layout.query(query)):
            t = e.dxftype()
            parts, how = _stand_in(doc, e)
            if not parts:
                summary["not_drawn"][t] = summary["not_drawn"].get(t, 0) + 1
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
