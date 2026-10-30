"""Comparing two versions of a drawing: the matching itself (pure ezdxf) and the compare view
through CADCraft (skipped when the runtime is not installed)."""

import tempfile
import unittest
from pathlib import Path

import ezdxf

from cad_eye_mcp import diff, gerber, paths
from cad_eye_mcp.session import Session, ViewerError

try:
    paths.cadcraft_exe()
    HAVE_ENGINE = True
except FileNotFoundError:
    HAVE_ENGINE = False


def bracket(rev: str, reverse: bool = False):
    """A bracket drawing in revision A or B. B edits A in known ways; `reverse` adds the model
    space entities in the opposite order, which gives them different handles."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name, color in (("輪廓", 7), ("標註", 3), ("NEW", 2)):
        doc.layers.add(name, color=color)
    bolt = doc.blocks.new("BOLT")
    bolt.add_circle((0, 0), 3 if rev == "A" else 4)                       # B: block definition edited
    bolt.add_line((-5, 0), (5, 0))
    title = doc.blocks.new("TITLE")
    title.add_lwpolyline([(0, 0), (80, 0), (80, 20), (0, 20)], close=True)
    title.add_attdef("DWG_NO", (5, 5), dxfattribs={"height": 5})
    msp = doc.modelspace()
    outline = {"layer": "輪廓"}
    steps = [
        lambda: msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True, dxfattribs=outline),
        *[(lambda i=i: msp.add_circle((20 + 40 * i, 80), 2, dxfattribs=outline)) for i in range(5)],
        lambda: msp.add_circle((50, 50), 10 if rev == "A" else 12, dxfattribs=outline),          # radius
        lambda: msp.add_circle((150, 50) if rev == "A" else (170, 60), 5, dxfattribs=outline),   # moved
        lambda: msp.add_text("REV " + rev, height=5, dxfattribs={"layer": "標註"}).set_placement((5, 105)),
        (lambda: msp.add_text("DELETE ME", height=5, dxfattribs={"layer": "標註"}).set_placement((100, 105)))
        if rev == "A" else (lambda: msp.add_line((0, -20), (200, -20), dxfattribs={"layer": "NEW"})),
        lambda: msp.add_blockref("BOLT", (20, 20)),
        lambda: msp.add_blockref("BOLT", (180, 20)),
        lambda: msp.add_blockref("TITLE", (120, -50)).add_auto_attribs({"DWG_NO": "M-100" if rev == "A" else "M-101"}),
    ]
    for step in reversed(steps) if reverse else steps:
        step()
    sheet = doc.layouts.new("Sheet")
    sheet.add_text("SCALE 1:1" if rev == "A" else "SCALE 1:2", height=3.5).set_placement((10, 10))
    return doc


def by_kind(changes):
    out = {}
    for c in changes:
        out.setdefault(c.kind, []).append(c)
    return out


class DiffTest(unittest.TestCase):
    def test_known_edits(self):
        changes, unchanged = diff.compare(bracket("B"), bracket("A"), 1e-6)
        kinds = by_kind(changes)
        self.assertEqual((len(kinds["added"]), len(kinds["removed"]), len(kinds["changed"])), (1, 1, 7))
        self.assertEqual((kinds["added"][0].type, kinds["added"][0].layer), ("LINE", "NEW"))
        self.assertEqual(kinds["removed"][0].type, "TEXT")
        what = [c.what for c in kinds["changed"]]
        self.assertIn({"moved": [20.0, 10.0]}, what)
        self.assertIn({"changed": {"radius": [10.0, 12.0]}}, what)
        self.assertIn({"changed": {"text": ["REV A", "REV B"]}}, what)
        self.assertEqual(sum(1 for w in what if w.get("changed", {}).get("block_content") == "different"), 2)
        attribs = [w["changed"]["attributes"] for w in what if "attributes" in w.get("changed", {})]
        self.assertEqual(attribs[0][0][0][1], "M-100")
        self.assertEqual(attribs[0][1][0][1], "M-101")
        sheet = [c for c in kinds["changed"] if c.space == "Sheet"]
        self.assertEqual(sheet[0].what, {"changed": {"text": ["SCALE 1:1", "SCALE 1:2"]}})
        self.assertEqual(unchanged, 6)  # outline + five holes

    def test_renumbered_copy_is_unchanged(self):
        a, b = bracket("A"), bracket("A", reverse=True)
        handles_a = {e.dxf.handle for e in a.modelspace()}
        handles_b = {e.dxf.handle for e in b.modelspace()}
        self.assertNotEqual(handles_a, handles_b)
        changes, unchanged = diff.compare(b, a, 1e-6)
        self.assertEqual(changes, [])
        self.assertEqual(unchanged, len(handles_a) + 1)  # + the sheet's text

    def test_edits_found_without_shared_handles(self):
        changes, _ = diff.compare(bracket("B", reverse=True), bracket("A"), 1e-6)
        what = [c.what for c in changes if c.kind == "changed"]
        self.assertIn({"moved": [20.0, 10.0]}, what)
        self.assertIn({"changed": {"text": ["REV A", "REV B"]}}, what)

    def test_tolerance(self):
        a, b = bracket("A"), bracket("A")
        next(iter(b.modelspace().query("CIRCLE"))).dxf.center = (20 + 1e-9, 80)
        self.assertEqual(diff.compare(b, a, 1e-6)[0], [])
        changes, _ = diff.compare(b, a, 1e-12)
        self.assertEqual([c.kind for c in changes], ["changed"])

    def test_regions_in_reading_order(self):
        changes, _ = diff.compare(bracket("B"), bracket("A"), 1e-6)
        regs = diff.regions(changes)
        self.assertTrue(all(c.region for c in changes))
        model = [r for r in regs if r.space == "Model"]
        tops = [r.box[1][1] for r in model]
        self.assertEqual(tops, sorted(tops, reverse=True))
        self.assertEqual(regs[-1].space, "Sheet")

    def test_moved_gerber_pad(self):
        from test_core import GERBER

        with tempfile.TemporaryDirectory() as tmp:
            for rev, text in (("a", GERBER), ("b", GERBER.replace("X5000000Y5000000D03", "X6000000Y5000000D03"))):
                (Path(tmp) / rev).mkdir()
                (Path(tmp) / rev / "board-F_Cu.gbr").write_text(text)
                gerber.convert(Path(tmp) / rev / "board-F_Cu.gbr", Path(tmp) / f"{rev}.dxf")
            changes, _ = diff.compare(ezdxf.readfile(Path(tmp) / "b.dxf"), ezdxf.readfile(Path(tmp) / "a.dxf"), 1e-6)
        self.assertEqual([(c.type, c.what) for c in changes], [("SOLID", {"moved": [1.0, 0.0]})])


@unittest.skipUnless(HAVE_ENGINE, "CADCraft runtime not installed (run install.ps1 / install.sh)")
class CompareViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a, cls.b = Path(cls.tmp.name) / "bracket_A.dxf", Path(cls.tmp.name) / "bracket_B.dxf"
        bracket("A").saveas(cls.a)
        bracket("B").saveas(cls.b)
        cls.s = Session()
        cls.s.open(str(cls.b))

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.s.compare(str(self.a))

    def test_summary_and_screen(self):
        summary = self.s.comparison_summary()
        self.assertEqual((summary["added"], summary["removed"], summary["changed"]), (1, 1, 7))
        self.assertEqual(summary["block_definitions_changed"], {"BOLT": "2 references"})
        png, state = self.s.frame()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn('"comparing_with":"bracket_A.dxf"', state)

    def test_region_lists_its_changes(self):
        first = self.s.goto_region(1)  # top left: the revision text
        self.assertEqual([c.get("changed") for c in first["changes"]], [{"text": ["REV A", "REV B"]}])
        n = next(c.region for c in self.s.drawing.comparison.changes if c.kind == "removed")
        removed = self.s.goto_region(n)["changes"][0]
        self.assertEqual((removed["kind"], removed["text"]), ("removed", "DELETE ME"))
        self.assertEqual(self.s.describe_baseline([removed["baseline_handle"]])[0]["text"], "DELETE ME")

    def test_click_reports_removed_entity(self):
        self.s.zoom_extents()
        hits = self.s.changes_at((110, 106.5), self.s.drawing.view.scale * 6)
        self.assertEqual([(h["kind"], h["type"]) for h in hits], [("removed", "TEXT")])

    def test_sheet_region_switches_layout(self):
        sheet = [r for r in self.s.drawing.comparison.regions if r.space == "Sheet"][0]
        self.s.goto_region(sheet.n)
        self.assertEqual(self.s.drawing.layout, "Sheet")
        self.s.set_layout("Model")

    def test_clear(self):
        self.s.clear_comparison()
        self.assertNotIn("comparing_with", self.s.state_text())
        with self.assertRaises(ViewerError):
            self.s.goto_region(1)


if __name__ == "__main__":
    unittest.main()
