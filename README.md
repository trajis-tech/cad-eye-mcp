# cad-eye-mcp

**An MCP server that gives an AI a CAD viewer it can operate itself.** Instead of being handed
one flat screenshot, the AI opens a drawing and then zooms, pans, clicks on objects, toggles
layers and measures, looking at the result after every step, the way a person studies a drawing.

Works with **DXF**, **DWG**, and PCB **Gerber / Excellon** files (single files, folders or zips).
Chinese (Taiwan, Hong Kong/Macau, Mainland), Japanese and English text all render with the right
glyphs.

[繁體中文說明](README.zh-TW.md)

![The AI's screen: rulers in drawing units, a grid, the clicked Japanese text and an MTEXT outlined in magenta, a minimap and a status bar](docs/screenshot.png)

## What the AI gets

Every action returns the **new screen** (PNG) plus a short JSON state:

- **Coordinate rulers** (top and left) in drawing units and a light grid, so positions can be
  read straight off the image.
- **Selection highlights** in magenta, with a ring around objects too small to see.
- **Markers and measurement lines** in orange.
- A **minimap** of the whole drawing with the current view when zoomed in.
- A **status bar** with the visible range, units per pixel, zoom, visible layers and the selection.

Tools accept positions as `px` (pixels of the image the AI was just shown) or `world` (drawing
units), so the AI can point at what it sees.

| Tool | What it does |
|---|---|
| `open_drawing` | Open a DXF, DWG, Gerber/drill file, folder or zip and show all of it |
| `look` | Show the current screen again |
| `zoom` / `zoom_window` / `zoom_extents` / `zoom_to` | Wheel-style zoom around a point, zoom to a rectangle, to everything, or to given entities |
| `pan` / `back` | Move the view; return to the previous view |
| `click` | Select what is under a point and get its exact data (type, layer, coordinates, text, block attributes, length...) plus nearby alternatives |
| `select_window` / `select` / `clear` | Box-select (with type/layer filters), select by handle, clear |
| `inspect` | Every DXF attribute of entities, effective colour, bounding box, length/area, block content |
| `find` | Find text (labels, reference designators, attributes) or entity types, then jump to them |
| `measure` | Distances, angles and area between points, or an entity's length/area |
| `layers` | List layers; show, hide or isolate them |
| `layout` | Switch between model space and paper-space layouts (sheets with title blocks, viewports, notes, tables) |
| `mark` | Put labelled markers on the drawing to point out findings |
| `screen_settings` | Screen size, grid, minimap, CJK glyph region |
| `cad_command` | Run any of CADCraft's 400+ AutoCAD-style commands (saving/exporting is blocked) |

Files are never modified.

## Install

The installer puts everything into `runtime/` inside this folder: a portable Python, the Python
packages, the CADCraft engine and CJK fonts. It does not touch your system Python, PATH, registry
or system fonts. Every download is pinned and checked against a SHA-256 in
[`runtime.lock.json`](runtime.lock.json). To uninstall, delete the folder.

```sh
git clone https://github.com/trajis-tech/cad-eye-mcp.git
cd cad-eye-mcp
```

**Windows** (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**Linux / macOS**:

```sh
./install.sh
```

Download size is about 150 MB. The installer ends with a self-check and prints the line to
register the server.

## Connect your MCP client

The server speaks MCP over stdio. The command is `cad-eye-mcp.cmd` (Windows) or `cad-eye-mcp`
(Linux/macOS) in this folder.

**Claude Code**

```sh
claude mcp add cad-eye -- "/path/to/cad-eye-mcp/cad-eye-mcp"        # Windows: ...\cad-eye-mcp.cmd
```

**Claude Desktop, Cursor, VS Code and other clients** (JSON config):

```json
{
  "mcpServers": {
    "cad-eye": { "command": "C:\\path\\to\\cad-eye-mcp\\cad-eye-mcp.cmd" }
  }
}
```

Then ask, for example: *"Open D:\\boards\\rev3.zip and check whether any trace on the top
copper passes closer than 0.2 mm to the mounting holes."*

## How it works

```
AI client ──MCP──▶ cad-eye-mcp (Python) ──MCP──▶ cadcraft-cli mcp (headless CADCraft)
                     │  view state, rulers, picking,          opens DXF/DWG, layer state,
                     │  highlights, minimap, measuring        renders PNG of a window
                     └─ ezdxf: exact entity data, geometry for picking and highlighting
                        gerbonara: Gerber/Excellon → layered DXF
```

- [CADCraft](https://github.com/storytold/cadcraft) is the view engine. It renders a window of the
  drawing (about 0.1 to 0.4 s per frame even on 200,000-entity boards), keeps layer visibility and
  reads DWG.
- [ezdxf](https://github.com/mozman/ezdxf) reads the same DXF. Both engines keep the original
  handles, so a pixel click maps to an exact entity. For DWG files, CADCraft writes a DXF copy to
  the cache for ezdxf.
- Gerber and drill files are converted with [gerbonara](https://gitlab.com/gerbolyze/gerbonara)
  into filled DXF geometry, one DXF layer per PCB layer (`F.Cu`, `B.Cu`, `In1.Cu`, `F.SilkS`,
  `Edge.Cuts`, `Drill.PTH`...). Mask, paste and bottom silkscreen start hidden. Conversions are
  cached.
- Entity types CADCraft cannot draw get stand-ins: custom objects of AutoCAD Mechanical, Civil 3D
  and similar applications (proxy entities) show their proxy graphics, the fallback drawing
  AutoCAD itself shows without the application; arc and large radial dimensions show their
  dimension geometry; ACIS solids with flat faces and meshes show a wireframe. Clicking a stand-in
  reports the original object under `drawn_for`, and anything left undrawn is listed under
  `not_drawn` when the drawing opens.
- Tables (`ACAD_TABLE`) can be clicked and searched; `inspect` returns their cell text row by row.
- Old DXF files without entity handles (R12 writers such as I-DEAS or CAM tools) are copied by
  CADCraft first, so both engines number the entities the same way.
- External references are loaded when the referenced files sit next to the drawing; missing ones
  are listed when the drawing opens.

### CJK text

The runtime includes Noto Sans CJK, one font collection that covers Traditional Chinese, Hong Kong
characters (HKSCS), Simplified Chinese and Japanese. On open, the server looks at the drawing's
text and picks the matching glyph forms (TC, HK, SC or JP). Set it yourself with
`screen_settings(cjk=...)` or the `CAD_EYE_CJK` environment variable. Text styles that name fonts
you do not have (SHX big fonts such as `chineset.shx`, `mingliu.ttc`, `simsun.ttc`...) still
render: missing characters come from the bundled font.

### Settings

| Environment variable | Meaning |
|---|---|
| `CAD_EYE_CJK` | `tc`, `hk`, `sc` or `jp`: fixed glyph region (turns off auto-detection) |
| `CAD_EYE_CADCRAFT` | Use this `cadcraft-cli` binary instead of the bundled one |
| `CAD_EYE_RUNTIME` | Use a runtime folder other than `./runtime` |
| `CAD_EYE_CACHE` | Cache folder for converted Gerber/DWG files (default: system temp) |

## Limitations

- Read-only viewer for now: nothing is saved. Editing is planned.
- In a paper-space layout, what viewports show is drawn but cannot be clicked; switch to `Model`
  to inspect it. If model space has nothing to draw, the first layout opens instead.
- Proxy graphics are what the authoring application chose to save: 3D-only objects (such as
  AutoCAD Mechanical parts) have none and stay invisible. ACIS solids with curved faces need a
  modeling kernel and are not drawn; wireframes of the others are plan views with hidden edges.
- DWG goes through CADCraft. Entity types it does not support yet are skipped (see its
  [file-format parity](https://github.com/storytold/cadcraft/blob/main/docs/file-format-parity.md)).
- Gerber "clear" (negative) polarity is drawn in a near-background colour on a `<layer>.clear`
  layer rather than cut out.
- Text boxes used for clicking and highlighting are estimated, and lean wide for CJK text.
- The first open of a large Gerber set converts it, which takes a while (about 20 s for a
  200,000-entity board); later opens use the cache and take a few seconds.
- On very large drawings the first `click`/`inspect` waits for ezdxf to finish reading the file
  (about 15 s for 200,000 entities). Rendering does not wait.
- Tested on Windows x64 and Linux x64. macOS and ARM builds are pinned but untested.

## Development

```sh
runtime/python/python -I scripts/fetch_samples.py           # sample DXF/DWG/Gerber files into samples/
runtime/python/python -I -m unittest discover -s tests      # tests (Linux/macOS: runtime/python/bin/python3)
runtime/python/python -I scripts/try_tools.py scenario.json out/   # drive the server like a client
```

`scenario.json` is a list of `[tool, {arguments}]` pairs. Every returned screen is saved to `out/`.

`tests/test_samples.py` runs fixed checks on the fetched samples (mechanical drawings, sheets with
tables, proxy objects with xrefs, ACIS solids, a 50,000+ entity board) and writes open times to
`samples/regress-timings.json`, flagging anything slower than 10 s. Without the samples, those
checks are skipped.

## License

MIT. Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
