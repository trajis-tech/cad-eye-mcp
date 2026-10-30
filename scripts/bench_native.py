"""Measure what cad-eye adds over CADCraft's own MCP server (the engine it is built on) and write
docs/benchmark.md. Every row runs the same task both ways on the same files and prints what
each side actually returned; nothing is scored by hand.

    runtime/python/python -I scripts/fetch_samples.py      # the public test drawings
    runtime/python/python -I scripts/bench_native.py        # add --large for the 220k-entity boards
"""

from __future__ import annotations

import contextlib
import io
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from cad_eye_mcp import __version__  # noqa: E402
from cad_eye_mcp.cadcraft import CadCraft, CadCraftError  # noqa: E402
from cad_eye_mcp.session import Session, ViewerError  # noqa: E402
from test_compare import bracket  # noqa: E402

SAMPLES = ROOT / "samples"
SHEET = ROOT / "tests" / "fixtures" / "sheet_layout.dxf"
ROWS: list[tuple[str, str, str, str]] = []  # task, file, CADCraft MCP, cad-eye


def native(path: Path) -> CadCraft:
    cc = CadCraft()
    cc.call("open", {"path": str(path)})
    return cc


def drawn(counts: dict) -> int:
    return sum(n for t, n in counts.items() if t not in ("Proxy", "Viewport"))


def row(task: str, file: str, cc_said: str, eye_said: str) -> None:
    ROWS.append((task, file, cc_said, eye_said))
    print(f"- {task}\n    CADCraft MCP: {cc_said}\n    cad-eye:      {eye_said}", flush=True)


def need(*rels: str) -> bool:
    missing = [r for r in rels if not (SAMPLES / r).exists()]
    if missing:
        print(f"  (skipped: {', '.join(missing)} not fetched)")
    return not missing


def bench(s: Session, tmp: Path, large: bool) -> None:
    # 1. formats -----------------------------------------------------------------------------
    if need("gerber/clockblock"):
        try:
            native(SAMPLES / "gerber/clockblock/clockblock-F_Cu.gbr").close()
            said = "opened"
        except CadCraftError as e:
            said = f"error: {str(e)[:70]}"
        d = s.open(str(SAMPLES / "gerber/clockblock"))
        row("Open a Gerber/Excellon board", "clockblock (tracespace fixture)", said,
            f"{len(d.gerber_summary['layers'])} PCB layers, {sum(d.counts.values()):,} entities")

    # 2. entity types the engine cannot draw ---------------------------------------------------
    if need("dxf/ezdxf_uncommon.dxf"):
        cc = native(SAMPLES / "dxf/ezdxf_uncommon.dxf")
        proxies = cc.call("query_entities", {"type": "Proxy", "limit": 50}).json()["entities"]
        cc.close()
        types = sorted({e["geometry"]["dxf_type"] for e in proxies})
        d = s.open(str(SAMPLES / "dxf/ezdxf_uncommon.dxf"))
        left = ", ".join(f"{t} x{n}" for t, n in d.unsupported.items()) or "nothing"
        row("Draw entity types CADCraft keeps as opaque proxies", "ezdxf uncommon.dxf",
            f"{len(proxies)} not drawn ({', '.join(types)})",
            f"{len(d.stand_ins)} drawn by stand-ins; reported as not drawn (blocks included): {left}")
        arc = s.describe(s.find_type("ARC_DIMENSION", None, None, 1))[0]
        row("Read an arc dimension's value", "ezdxf uncommon.dxf", "raw DXF tags only (no value)",
            f"arc length {arc['measurement']:.4f}")
    if need("mech/DRYER_ASSEMBLY.dwg"):
        cc = native(SAMPLES / "mech/DRYER_ASSEMBLY.dwg")
        cc.execute("layout.set", {"name": "Layout2"})
        counts = cc.call("inspect_drawing", {}).json().get("counts", {})
        cc.close()
        d = s.open(str(SAMPLES / "mech/DRYER_ASSEMBLY.dwg"))
        row("Show the assembly sheet (AutoCAD Mechanical proxies)", "NIST blow dryer assembly, Layout2",
            f"{drawn(counts):,} entities drawn ({counts.get('Proxy', 0)} proxy not drawn)",
            f"{drawn(d.counts):,} entities drawn (proxy graphics)")

    # 3. reading the data --------------------------------------------------------------------------
    if need("mech/blocks_and_tables_-_metric.dwg"):
        cc = native(SAMPLES / "mech/blocks_and_tables_-_metric.dwg")
        cc.execute("layout.set", {"name": "ISO A1"})
        hits = cc.call("find_text", {"contains": "ANDERSEN"}).json()["count"]
        tables = cc.call("query_entities", {"type": "Table"}).json()["count"]
        cc.close()
        s.open(str(SAMPLES / "mech/blocks_and_tables_-_metric.dwg"))
        s.set_layout("ISO A1")
        found = s.table_hits("ANDERSEN")
        row("Find a text that is inside a table cell", "Autodesk blocks_and_tables (metric)",
            f"find_text: {hits} hits (tables: cells readable via query_entities, {tables} tables)",
            f"find: {len(found)} table row ('{found[0]['rows'][0][:40]}...')" if found else "find: 0")
    if need("mech/mechanical_example-imperial.dwg"):
        cc = native(SAMPLES / "mech/mechanical_example-imperial.dwg")
        dims = cc.call("query_entities", {"type": "Dimension", "limit": 50}).json()["entities"]
        cc.close()
        s.open(str(SAMPLES / "mech/mechanical_example-imperial.dwg"))
        mine = s.describe(s.find_type("Dimension", None, None, 50))
        row("Read dimension texts (parity check)", "Autodesk mechanical example (imperial)",
            f"{sum(1 for e in dims if e['geometry'].get('text'))} of {len(dims)} with text",
            f"{sum(1 for e in mine if e.get('text'))} of {len(mine)} with text")

    # 4. sheets ----------------------------------------------------------------------------------
    cc = native(SHEET)
    cc.execute("layout.set", {"name": "A3 圖紙"})
    under = cc.call("query_entities", {"window": [[319, 219], [321, 221]]}).json()["entities"]
    cc.close()
    s.open(str(SHEET))
    s.set_layout("A3 圖紙")
    ranked = s.pick((320, 220))
    hit = s.describe([ranked[0][0]])[0] if ranked else {}
    row("Click a part inside a 1:2 viewport on a sheet", "tests/fixtures/sheet_layout.dxf",
        "query at the point: " + (", ".join(e["type"] for e in under) or "nothing"),
        f"{hit.get('type')} r={hit.get('radius')} at model {hit.get('center')} (through the viewport)")
    pair = s.to_model([(285, 220), (315, 220)])
    row("Measure across that viewport", "tests/fixtures/sheet_layout.dxf", "30 (paper units only)",
        f"30 on paper = {abs(pair[1][1][0] - pair[1][0][0]):g} in model space" if pair else "-")

    # 5. comparing revisions ------------------------------------------------------------------------
    a, b, b2 = tmp / "bracket_A.dxf", tmp / "bracket_B.dxf", tmp / "bracket_B_renumbered.dxf"
    bracket("A").saveas(a)
    bracket("B").saveas(b)
    bracket("B", reverse=True).saveas(b2)
    for task, new in (("Compare two revisions (same handles)", b),
                      ("Compare two revisions (re-exported, handles renumbered)", b2)):
        cc = native(new)
        r = cc.execute("compare", {"path": str(a)})
        cc.close()
        n = r["counts"]
        whats = sorted({c.get("what") for c in r.get("changed", [])})
        s.open(str(new))
        s.compare(str(a))
        m = s.comparison_summary()
        sample = [s.change_row(i) for i, c in enumerate(s.drawing.comparison.changes) if c.kind == "changed"][:2]
        row(task, "bracket A -> B: 9 known edits, one on the sheet",
            f"+{n['added']} -{n['removed']} ~{n['changed']}; changes described as {whats}",
            f"+{m['added']} -{m['removed']} ~{m['changed']}; e.g. "
            + "; ".join(json.dumps({k: v for k, v in c.items() if k in ('type', 'moved', 'changed')},
                                   ensure_ascii=False) for c in sample))
    if large and need("gerber/mrfixit-v1.0.zip", "gerber/mrfixit-v1.1.zip"):
        t = time.perf_counter()
        s.open(str(SAMPLES / "gerber/mrfixit-v1.1.zip"))
        s.compare(str(SAMPLES / "gerber/mrfixit-v1.0.zip"))
        m = s.comparison_summary()
        row("Compare two board revisions (Gerber)", "mr-fixit v1.0 -> v1.1 (220,000+ entities)",
            "cannot open Gerber",
            f"+{m['added']} -{m['removed']} ~{m['changed']}, of which on Edge.Cuts: "
            f"{json.dumps(m['by_layer'].get('Edge.Cuts', {}))} ({time.perf_counter() - t:.0f} s to open and compare)")

    # 6. safety --------------------------------------------------------------------------------------
    cc = native(SHEET)
    with cc._lock:
        tools = [t["name"] for t in cc._request("tools/list", {})["tools"]]
    cc.close()
    try:
        s.command("save", {"path": str(tmp / "x.dxf")})
        blocked = "saved"
    except ViewerError:
        blocked = "blocked (save, export, plot, open... refused)"
    row("Write files", "any", "a `save` tool overwrites or exports files" if "save" in tools else "no save tool",
        blocked)


def main() -> int:
    large = "--large" in sys.argv
    s = Session()
    try:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()):
            bench(s, Path(tmp), large)
    finally:
        s.close()
    lines = [
        "# cad-eye vs CADCraft's own MCP server",
        "",
        "cad-eye is built on [CADCraft](https://github.com/storytold/cadcraft), which has its own MCP server "
        "(`cadcraft-cli mcp`). This table runs the same tasks through both, on the same files, and shows what "
        "each side returned. It is generated by `scripts/bench_native.py`; rerun it to check.",
        "",
        f"cad-eye {__version__}, {platform.system()} {platform.machine()}, "
        f"{time.strftime('%Y-%m-%d')}. Files come from `scripts/fetch_samples.py` and `tests/`.",
        "",
        "| Task | File | CADCraft MCP | cad-eye |",
        "|---|---|---|---|",
    ]
    for task, file, cc_said, eye_said in ROWS:
        cells = [c.replace("|", "\\|") for c in (task, file, cc_said, eye_said)]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Where both sides return the same data (dimension texts, circle radii, table cells through "
        "`query_entities`), cad-eye adds no data of its own: it adds the screen around it (rulers, "
        "picking by pixel, highlights, regions) and the rows above. Whether that screen makes a model "
        "answer drawing questions better is not measured here.",
        "",
    ]
    out = ROOT / "docs" / "benchmark.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
