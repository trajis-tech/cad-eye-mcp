"""MCP tools: an AI-operated, headless CAD viewer for DXF, DWG and Gerber/Excellon files."""

from __future__ import annotations

import functools
import json
import math
from typing import Annotated, Any, Literal

import numpy as np
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, deep
from .cadcraft import CadCraftError
from .session import Session, ViewerError
from .view import normalize_box

INSTRUCTIONS = """\
cad-eye is a CAD viewer you operate yourself, like a person at a screen. Open a drawing, then
look at it, zoom, pan, click on things and toggle layers. Every action returns the new screen as an
image plus a JSON state.

How to read the screen: the image has coordinate rulers (top and left) in drawing units, a light
grid at the ruler ticks, a status bar at the bottom, and a minimap (top-right) when zoomed in.
Selected entities are outlined in magenta; tiny ones get a magenta ring. Orange crosses are markers.

Coordinates: tools take `coords="px"` (pixels of the last image you were shown, origin top-left
of the whole image) or `coords="world"` (drawing units, y up, as on the rulers). Prefer world
coordinates read from the rulers when you need precision.

Good habits: start from the overview, zoom into regions before judging small details (text,
pads, dimensions), click an object to get its exact data instead of estimating from pixels, and
isolate layers when a region is crowded. If the drawing has paper-space layouts (see the notes
from open_drawing), switch to them with layout: sheets, title blocks and drawing views are often
there rather than in model space; clicking or measuring inside a viewport there reaches the
model-space part behind it, at the viewport's scale. Not everything in a file can always be
drawn (custom objects, some 3D solids, missing xrefs): the status bar says so, and check tells
what is missing, where and why, so run check before concluding that something is absent. To
review a revision, open the new version and call compare
with the old one: differences are drawn in colour and grouped into numbered regions to visit
one by one. Files are never modified."""

mcp = MCPServer(name="cad-eye", title="cad-eye CAD viewer", version=__version__, instructions=INSTRUCTIONS)
session = Session()

Coords = Annotated[Literal["px", "world"], Field(description="'px' = pixels of the last image, 'world' = drawing units")]
VIEW_TOOL = ToolAnnotations(readOnlyHint=True, idempotentHint=False, openWorldHint=False)
READ_TOOL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


def _screen(extra: dict | None = None) -> list:
    png, state = session.frame(extra)
    return [Image(data=png, format="png"), state]


def _guard(fn):
    """Report failures to the AI with their message (the SDK would hide other exceptions as
    internal errors)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ViewerError, CadCraftError, ValueError, FileNotFoundError, KeyError) as e:
            raise ToolError(str(e)) from None
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - e.g. an unreadable entity in the drawing
            raise ToolError(f"{type(e).__name__}: {e}") from None

    return wrapper


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def open_drawing(
    path: Annotated[str, Field(description="Absolute path to a .dxf or .dwg file, a Gerber/Excellon file, "
                                           "a folder of Gerber files, or a .zip of them")],
) -> list:
    """Open a drawing and show the whole of it. Replaces the drawing open before."""
    d = session.open(path)
    info: dict[str, Any] = {
        "opened": d.kind,
        "entity_counts": d.counts,
        "extents": [list(d.extents[0]), list(d.extents[1])],
        "layers": [l["name"] for l in d.layers],
    }
    if d.gerber_summary:
        info["pcb_layers"] = d.gerber_summary["layers"]
        info["hint"] = "PCB layers are DXF layers; mask/paste/bottom silk start hidden (see layers)."
    if d.unsupported:
        info["not_drawn"] = d.unsupported
    if d.engine_skipped:
        info["not_read_by_engine"] = d.engine_skipped
    if d.notes:
        info["notes"] = d.notes
    if d.unsupported or d.engine_skipped or d.missing_xrefs:
        info["check"] = "Part of this file is not on screen: run check for what, where and why."
    return _screen(info)


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def look() -> list:
    """Show the current screen again (after changing options, or to refresh your view)."""
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def zoom(
    factor: Annotated[float, Field(description="> 1 zooms in (2 = twice as close), < 1 zooms out")] = 2.0,
    x: Annotated[float | None, Field(description="Point that stays fixed on screen; default: view centre")] = None,
    y: float | None = None,
    coords: Coords = "px",
) -> list:
    """Zoom in or out around a point, like the mouse wheel over that spot."""
    at = session.point(x, y, coords) if x is not None and y is not None else None
    session.zoom(factor, at)
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def zoom_window(x0: float, y0: float, x1: float, y1: float, coords: Coords = "px") -> list:
    """Zoom so a rectangle (two opposite corners) fills the screen, like a zoom-window drag."""
    session.zoom_window(session.point(x0, y0, coords), session.point(x1, y1, coords))
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def zoom_extents() -> list:
    """Show the whole drawing."""
    session.zoom_extents()
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def zoom_to(
    handles: Annotated[list[str] | None, Field(description="Entity handles; default: the current selection")] = None,
    margin: Annotated[float, Field(description="Space around the objects, as a fraction of their size")] = 0.3,
) -> list:
    """Frame specific entities (e.g. results of find, or the selection)."""
    hs = handles or session.drawing.selection
    if not hs:
        raise ValueError("Nothing to zoom to: pass handles or select something first.")
    box = session.zoom_to(hs, margin)
    return _screen({"framed": [list(box[0]), list(box[1])]})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def pan(
    direction: Annotated[Literal["left", "right", "up", "down"] | None,
                         Field(description="Move the view this way by `amount` screens")] = None,
    amount: Annotated[float, Field(description="Fraction of the screen to move for `direction`")] = 0.5,
    dx: Annotated[float | None, Field(description="Or move the view by dx, dy (in `coords` units)")] = None,
    dy: float | None = None,
    coords: Coords = "px",
) -> list:
    """Move the view without changing zoom. 'right' shows what is to the right of the screen."""
    v = session.drawing.view
    if direction:
        step_x, step_y = v.width * amount, v.height * amount
        ddx, ddy = {"left": (-step_x, 0), "right": (step_x, 0), "up": (0, step_y), "down": (0, -step_y)}[direction]
    elif dx is not None or dy is not None:
        ddx, ddy = dx or 0.0, dy or 0.0
        if coords == "px":  # dragging the view by pixels moves it the other way in y
            ddx, ddy = ddx * v.scale, -ddy * v.scale
    else:
        raise ValueError("Give a direction, or dx/dy.")
    session.pan(ddx, ddy)
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def back() -> list:
    """Return to the previous view (zoom previous)."""
    moved = session.back()
    return _screen(None if moved else {"note": "no earlier view"})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def click(
    x: float,
    y: float,
    coords: Coords = "px",
    add: Annotated[bool, Field(description="Add to the selection instead of replacing it")] = False,
    tolerance_px: Annotated[float, Field(description="Pick box half-size in screen pixels")] = 6,
) -> list:
    """Click on the drawing: select the object under the point and report exactly what it is
    (type, layer, coordinates, text, block attributes, size...). Nearby alternatives are listed;
    select one with select(handles=[...]) if the pick was not the one you wanted."""
    p = session.point(x, y, coords)
    ranked = session.pick(p, tolerance_px)
    d = session.drawing
    changes =session.changes_at(p, tolerance_px * d.view.scale)
    if not ranked:
        if not add:
            d.selection = []
        out = {"clicked_at": [round(p[0], 6), round(p[1], 6)], "picked": None,
               "note": "nothing visible within the pick box; zoom in or raise tolerance_px"}
        if changes:
            out["changes_here"] = changes
            out["note"] = "only a removed entity of the baseline is here (see changes_here)"
        return _screen(out)
    best = ranked[0][0]
    d.selection = (d.selection + [best]) if add and best not in d.selection else [best]
    picked = session.describe([best], full=True)[0]
    through = session.through_viewport(best, p)
    if through:
        picked["through_viewport"] = through
    alts = []
    for h, dist in ranked[1:8]:
        s = session.describe([h])[0]
        s["distance"] = None if math.isinf(dist) else round(dist, 6)
        alts.append(s)
    out = {"clicked_at": [round(p[0], 6), round(p[1], 6)], "picked": picked, "also_here": alts}
    if changes:
        out["changes_here"] = changes
    return _screen(out)


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def select_window(
    x0: float, y0: float, x1: float, y1: float,
    coords: Coords = "px",
    crossing: Annotated[bool, Field(description="True: touching the box counts; False: must be fully inside")] = True,
    type: Annotated[str | None, Field(description="Only this entity type, e.g. Line, Circle, Text, Polyline, Insert")] = None,
    layer: str | None = None,
) -> list:
    """Box-select visible objects and summarise what is inside (counts by type and layer)."""
    box = normalize_box(session.point(x0, y0, coords), session.point(x1, y1, coords))
    hs = session.candidates(box, crossing=crossing, type_=type, layer=layer)
    d = session.drawing
    d.selection = hs
    session.annotate(boxes=[box])
    by_type: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    items = session.describe(hs[:40])
    for s in session.describe(hs[:2000]):
        by_type[s.get("type", "?")] = by_type.get(s.get("type", "?"), 0) + 1
        by_layer[s.get("layer", "?")] = by_layer.get(s.get("layer", "?"), 0) + 1
    return _screen({"selected": len(hs), "by_type": by_type, "by_layer": by_layer, "first": items})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def select(
    handles: Annotated[list[str], Field(description="Entity handles to select (replaces the selection)")],
) -> list:
    """Select entities by handle and highlight them."""
    d = session.drawing
    d.selection = [h.upper() for h in handles]
    return _screen({"selected": session.describe(d.selection[:40])})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def clear() -> list:
    """Clear the selection, markers and boxes."""
    session.drawing.selection = []
    session.annotate()
    return _screen()


@mcp.tool(annotations=READ_TOOL, structured_output=False)
@_guard
def inspect(
    handles: Annotated[list[str] | None, Field(description="Entity handles; default: the selection")] = None,
    baseline: Annotated[bool, Field(description="Look the handles up in the baseline drawing of the "
                                                "current comparison (baseline_handle values)")] = False,
) -> str:
    """Full data of entities: every DXF attribute, effective colour, bounding box, length/area,
    block content and attributes. Text only, no image."""
    hs = handles or ([] if baseline else session.drawing.selection)
    if not hs:
        raise ValueError("Nothing to inspect: pass handles or select something first.")
    rows = session.describe_baseline(hs[:25]) if baseline else session.describe(hs[:25], full=True)
    return json.dumps(rows, ensure_ascii=False, indent=1, default=str)


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def find(
    text: Annotated[str | None, Field(description="Find texts/attributes containing this (any case)")] = None,
    type: Annotated[str | None, Field(description="Or find entities of this type, e.g. Circle, Insert, Dimension")] = None,
    layer: str | None = None,
    in_view: Annotated[bool, Field(description="Only search the current view")] = False,
    show: Annotated[bool, Field(description="Select the results and zoom to them")] = True,
    limit: int = 50,
) -> list:
    """Search the drawing for text (labels, reference designators, notes) or for entity types,
    then optionally jump to and highlight the results."""
    d = session.drawing
    window = [list(d.view.window[0]), list(d.view.window[1])] if in_view else None
    results: list[dict] = []
    if text:
        args: dict[str, Any] = {"contains": text, "limit": limit}
        if layer:
            args["layer"] = layer
        if window:
            args["window"] = window
        res = session.cc.call("find_text", args).json() or {}
        for t in res.get("texts", res.get("entities", [])):
            results.append({k: t.get(k) for k in ("handle", "text", "at", "layer", "height") if k in t})
        total = res.get("count", len(results))
        if not window:
            tables = session.table_hits(text, layer)
            results += tables
            total += len(tables)
    elif type or layer:
        hs = session.find_type(type, layer, d.view.window if in_view else None, limit)
        results = session.describe(hs[:limit])
        total = len(hs)
    else:
        raise ValueError("Give text, type or layer to search for.")
    extra: dict[str, Any] = {"found": total, "results": results[:limit]}
    if text and not total and d.kind == "gerber":
        extra["note"] = ("Gerber files hold no text entities: silkscreen labels are plain geometry. "
                         "Read them from the image instead (zoom in on the silkscreen layers).")
    handles = [r["handle"] for r in results if r.get("handle")]
    if show and handles:
        session.annotate()
        d.selection = handles
        try:
            session.zoom_to(handles, margin=0.4)
        except ViewerError:
            pass
    return _screen(extra)


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def measure(
    points: Annotated[list[list[float]] | None, Field(description="Two or more points [[x,y], ...]")] = None,
    coords: Coords = "world",
    handle: Annotated[str | None, Field(description="Or measure an entity: length, area, radius")] = None,
) -> list:
    """Measure distances (segment lengths, dx/dy, angles, total) between points, the area if three
    or more points are given, or an entity's length/area. Points are marked on screen."""
    d = session.drawing
    if handle:
        doc = session.doc()
        e = deep.entity_by_handle(doc, handle) if doc is not None else None
        if e is None:
            raise ValueError(f"Unknown handle {handle}")
        d.selection = [e.dxf.handle]
        return _screen({"measure": {**deep.summary(e), **deep.measure_entity(e)}})
    if not points or len(points) < 2:
        raise ValueError("Give at least two points, or a handle.")
    pts = [session.point(p[0], p[1], coords) for p in points]
    segs, total = [], 0.0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dist = math.hypot(bx - ax, by - ay)
        total += dist
        segs.append({"from": [round(ax, 6), round(ay, 6)], "to": [round(bx, 6), round(by, 6)],
                     "distance": round(dist, 6), "dx": round(bx - ax, 6), "dy": round(by - ay, 6),
                     "angle_deg": round(math.degrees(math.atan2(by - ay, bx - ax)), 4)})
    out: dict[str, Any] = {"segments": segs, "total": round(total, 6), "units": d.units}
    if len(pts) >= 3:
        out["area_closed"] = round(deep.polygon_area(np.asarray(pts)), 6)
    model = session.to_model(pts)
    if model is not None:  # all points inside one viewport: the same measure on the part itself
        vp, mpts = model
        mlen = [math.dist(a, b) for a, b in zip(mpts, mpts[1:])]
        out["model_space"] = {"viewport": vp.dxf.handle, "paper_units_per_model_unit": round(vp.get_scale(), 6),
                              "points": [[round(x, 6), round(y, 6)] for x, y in mpts],
                              "distances": [round(v, 6) for v in mlen], "total": round(sum(mlen), 6),
                              "units": d.model_units}
        if len(mpts) >= 3:
            out["model_space"]["area_closed"] = round(deep.polygon_area(np.asarray(mpts)), 6)
    session.annotate(markers=[(p, f"P{i + 1}") for i, p in enumerate(pts)], lines=[pts])
    return _screen({"measure": out})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def layers(
    action: Annotated[Literal["list", "show", "hide", "isolate", "show_all"],
                      Field(description="list: table of layers; isolate: show only `names`")] = "list",
    names: Annotated[list[str] | None, Field(description="Layer names for show/hide/isolate")] = None,
) -> list | str:
    """List layers (visibility, colour, entity count) or change which layers are shown."""
    if action == "list":
        return json.dumps(session.layer_rows(), ensure_ascii=False, indent=1)
    if action != "show_all" and not names:
        raise ValueError("Give layer names.")
    changed = session.set_layers(action, names or [])
    return _screen({"changed_layers": changed[:50]})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def layout(
    name: Annotated[str | None, Field(description="'Model' or a paper-space layout name; omit to list them")] = None,
) -> list | str:
    """Switch between model space and paper-space layouts (sheets with title blocks, drawing views
    through viewports, notes and parts lists), or list the layouts."""
    d = session.drawing
    if not name:
        return json.dumps({"current": d.layout, "layouts": ["Model"] + d.layouts}, ensure_ascii=False)
    session.set_layout(name)
    d = session.drawing
    extra: dict[str, Any] = {"layout": d.layout, "units": d.units, "entity_counts": d.counts,
                             "extents": [list(d.extents[0]), list(d.extents[1])]}
    if d.counts.get("Viewport"):
        extra["hint"] = ("Viewports show model space through a window. Clicking inside one picks the model "
                         "entity (reported with through_viewport), and measuring inside one also gives "
                         "model-space distances. Box selection and find work on the sheet only.")
    return _screen(extra)


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def check(
    show: Annotated[bool, Field(description="Mark the problems that have a position in the current view's "
                                            "space (orange markers with a box)")] = True,
) -> list:
    """Completeness and integrity report: what of the file the screen does not show, and why.
    Lists entities that cannot be drawn (with the reason and their handles), entities the engine
    could not read, external references and images whose files are missing, hidden layers,
    invisible entities, layers frozen in viewports, content in other layouts, problems in the
    file's structure (audit, nothing is changed) and, for PCBs, Gerber objects or files that
    were not converted. Starts with a verdict; run it before concluding that something is
    absent from a drawing."""
    report, marks = session.check()
    if show and marks:
        session.annotate(markers=[(p, label) for p, label, _ in marks], boxes=[b for _, _, b in marks if b])
    return _screen({"check": report})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def compare(
    baseline: Annotated[str | None, Field(description="Absolute path of the other version to compare the "
                                                      "open drawing with, usually the previous revision "
                                                      "(DXF, DWG, or Gerber file/folder/zip)")] = None,
    region: Annotated[int | None, Field(description="Zoom to this numbered region of the current "
                                                    "comparison and list its changes")] = None,
    tolerance: Annotated[float | None, Field(description="Coordinates closer than this (drawing units) "
                                                         "count as equal; default 1e-7 of the drawing size")] = None,
    clear: Annotated[bool, Field(description="End the comparison and show the drawing normally")] = False,
) -> list:
    """Compare the open drawing with another version of it, like a drawing-compare tool. The
    drawing is dimmed and the differences drawn over it: added entities green, removed ones red
    (drawn from the baseline), changed ones blue with their old version in thin red. Changes are
    grouped into numbered regions (#1, #2... on screen); call compare(region=n) to zoom to one and
    read exactly what changed (moved by, old -> new attribute values, edited text, block
    definitions). Entities are matched by content, so files re-saved by other programs compare too."""
    d = session.drawing
    if clear:
        session.clear_comparison()
        return _screen({"compare": "cleared"})
    if baseline:
        session.compare(baseline, tolerance)
        session.zoom_extents()
        return _screen({"compare": session.comparison_summary()})
    if region is not None:
        return _screen({"compare_region": session.goto_region(region)})
    if d.comparison is None:
        raise ValueError("Give the baseline path of the version to compare with.")
    return _screen({"compare": session.comparison_summary()})


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def mark(
    points: Annotated[list[list[float]], Field(description="Points [[x,y], ...] to mark")],
    labels: list[str] | None = None,
    coords: Coords = "world",
    add: Annotated[bool, Field(description="Keep the markers and measurement lines already shown")] = False,
) -> list:
    """Put labelled orange markers on the drawing (e.g. to point out findings), then show it."""
    labels = labels or []
    d = session.drawing
    new = [(session.point(p[0], p[1], coords), labels[i] if i < len(labels) else "") for i, p in enumerate(points)]
    if add:
        session.annotate(markers=d.markers + new, boxes=d.boxes, lines=d.lines)
    else:
        session.annotate(markers=new)
    return _screen()


@mcp.tool(annotations=VIEW_TOOL, structured_output=False)
@_guard
def screen_settings(
    width_px: Annotated[int | None, Field(description="Drawing area width (320-1800)")] = None,
    height_px: Annotated[int | None, Field(description="Drawing area height (240-1400)")] = None,
    grid: bool | None = None,
    minimap: bool | None = None,
    cjk: Annotated[Literal["tc", "hk", "sc", "jp"] | None,
                   Field(description="Glyph forms for CJK text: tc Taiwan, hk Hong Kong/Macau, "
                                     "sc Mainland China, jp Japan. Chosen automatically on open.")] = None,
) -> list:
    """Change the screen: size of the drawing area, grid lines, minimap, CJK glyph region."""
    if cjk:
        session.set_cjk(cjk)
    if width_px or height_px:
        w, h = session.size
        session.resize(width_px or w, height_px or h)
    if grid is not None:
        session.grid = grid
    if minimap is not None:
        session.minimap = minimap
    return _screen()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
          structured_output=False)
@_guard
def cad_command(
    command: Annotated[str, Field(description="CADCraft command id, e.g. list, dist, area, id, "
                                              "massprop, count, layiso, draworder")],
    params: Annotated[dict | None, Field(description="JSON parameters of the command")] = None,
) -> str:
    """Run a CADCraft command directly (its catalog has 400+ AutoCAD-style commands). Use for
    inquiries the other tools do not cover. Changes stay in the viewer session and are never
    saved; any command that writes or opens a file is refused."""
    return json.dumps(session.command(command, params), ensure_ascii=False, indent=1, default=str)
