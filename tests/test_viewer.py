"""End-to-end tests through CADCraft (skipped when the runtime is not installed)."""

import io
import math
import tempfile
import unittest
from pathlib import Path

import ezdxf
from PIL import Image

from cad_eye_mcp import paths
from cad_eye_mcp.session import Session, ViewerError

FIXTURE = Path(__file__).parent / "fixtures" / "cjk_mixed.dxf"
SHEET = Path(__file__).parent / "fixtures" / "sheet_layout.dxf"
SOLIDS = Path(__file__).parent / "fixtures" / "solids.dxf"

try:
    paths.cadcraft_exe()
    HAVE_ENGINE = True
except FileNotFoundError:
    HAVE_ENGINE = False


@unittest.skipUnless(HAVE_ENGINE, "CADCraft runtime not installed (run install.ps1 / install.sh)")
class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Session()
        cls.s.open(str(FIXTURE))

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_frame_size_and_state(self):
        png, state = self.s.frame()
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, self.s.drawing.view.frame_size)
        self.assertIn('"units":"mm"', state)

    def test_click_on_text_selects_it(self):
        self.s.zoom_extents()
        ranked = self.s.pick((1500, 2550))  # inside the Japanese text line
        self.assertTrue(ranked)
        picked = self.s.describe([ranked[0][0]])[0]
        self.assertEqual(picked["type"], "TEXT")
        self.assertIn("ひらがな", picked["text"])

    def test_click_on_far_end_of_cjk_text(self):
        # CADCraft's own text extents stop short of where wide CJK glyphs are drawn
        self.s.zoom_extents()
        ranked = self.s.pick((8300, 2550))
        self.assertTrue(ranked)
        self.assertIn("寸法", self.s.describe([ranked[0][0]])[0]["text"])

    def test_pick_ignores_far_block(self):
        self.s.zoom_extents()
        self.assertEqual(self.s.pick((5000, -5000)), [])

    def test_layers_hide_and_restore(self):
        self.s.set_layers("hide", ["填充"])
        self.assertIn("填充", [l["name"] for l in self.s.drawing.layers if not l["on"]])
        self.s.set_layers("show_all", [])
        self.assertTrue(all(l["on"] for l in self.s.drawing.layers))

    def test_find_text_in_block_attribute(self):
        res = self.s.cc.call("find_text", {"contains": "A-101"}).json()
        self.assertEqual(res["count"], 1)

    def test_writes_are_blocked(self):
        with self.assertRaises(Exception):
            self.s.command("save", {"path": "x.dxf"})

    def test_zoom_window_then_back(self):
        before = self.s.drawing.view
        self.s.zoom_window((0, 0), (1000, 1000))
        self.assertLess(self.s.drawing.view.height, before.height)
        self.assertTrue(self.s.back())
        self.assertEqual(self.s.drawing.view, before)


@unittest.skipUnless(HAVE_ENGINE, "CADCraft runtime not installed (run install.ps1 / install.sh)")
class LayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Session()
        cls.d = cls.s.open(str(SHEET))

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_layouts_are_announced(self):
        self.assertEqual(self.d.layouts, ["Layout1", "A3 圖紙"])  # ezdxf adds Layout1 to new drawings
        self.assertTrue(any("layout" in n for n in self.d.notes))

    def test_switch_to_sheet_and_back(self):
        self.s.set_layout("a3 圖紙")
        d = self.s.drawing
        self.assertEqual((d.layout, d.units), ("A3 圖紙", "mm"))
        self.assertGreater(d.extents[1][0], 400)  # the 420 mm sheet border
        ranked = self.s.pick((305, 32))
        self.assertTrue(ranked)
        self.assertIn("M-001", self.s.describe([ranked[0][0]])[0]["text"])
        self.assertIn('"layout":"A3 圖紙"', self.s.state_text())
        self.s.set_layout("Model")
        self.assertEqual(self.s.drawing.layout, "Model")
        self.assertLess(self.s.drawing.extents[1][0], 200)

    def test_unknown_layout(self):
        with self.assertRaises(ViewerError):
            self.s.set_layout("Layout9")

    def test_click_and_measure_through_viewport(self):
        self.s.set_layout("A3 圖紙")
        try:
            ranked = self.s.pick((320, 220))  # right hole's edge, in the half-scale detail view
            self.assertTrue(ranked)
            h = ranked[0][0]
            row = self.s.describe([h])[0]
            self.assertEqual((row["type"], row["center"], row["radius"]), ("CIRCLE", [90.0, 30.0], 10))
            through = self.s.through_viewport(h, (320, 220))
            self.assertEqual((through["paper_units_per_model_unit"], through["model_point"]), (0.5, [100.0, 30.0]))
            box = self.s.bbox_of([h])  # where the sheet shows it
            self.assertAlmostEqual(box[0][0], 310, places=3)
            self.assertAlmostEqual(box[1][0], 320, places=3)
            self.s.drawing.selection = [h]
            png, _ = self.s.frame()
            self.assertTrue(png.startswith(b"\x89PNG"))
            vp, model = self.s.to_model([(285, 220), (315, 220)])
            self.assertAlmostEqual(math.dist(*model), 60)  # 30 mm on paper at 1:2
            self.assertIsNone(self.s.to_model([(285, 220), (100, 150)]))  # two different viewports
        finally:
            self.s.set_layout("Model")


@unittest.skipUnless(HAVE_ENGINE, "CADCraft runtime not installed (run install.ps1 / install.sh)")
class SolidsTest(unittest.TestCase):
    """CADCraft does not draw ACIS solids: flat-faced ones become wireframes, the rest is reported."""

    @classmethod
    def setUpClass(cls):
        cls.s = Session()
        cls.d = cls.s.open(str(SOLIDS))

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_cube_is_drawn_and_broken_solid_reported(self):
        self.assertEqual(self.d.layout, "Model")
        self.assertEqual(self.d.unsupported, {"3DSOLID": 1})
        self.assertTrue(any("wireframe" in n for n in self.d.notes))

    def test_click_on_wireframe_names_the_solid(self):
        self.s.zoom_extents()
        ranked = self.s.pick((10, 0))  # the cube's right edge
        self.assertTrue(ranked)
        self.assertEqual(self.s.describe([ranked[0][0]])[0]["drawn_for"], {"handle": "2F", "object": "3DSOLID"})

    def test_find_by_type_lists_both_solids(self):
        self.assertEqual(sorted(self.s.find_type("3DSOLID", None, None, 10)), ["2F", "30"])

    def test_nothing_drawable_stays_in_model_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = ezdxf.new("R2018")
            doc.modelspace().add_3dsolid().sab = b"ACIS BinaryFile but not really"
            path = Path(tmp) / "only_broken.dxf"
            doc.saveas(path)
            s = Session()
            try:
                d = s.open(str(path))
                self.assertEqual(d.layout, "Model")
                self.assertTrue(any(n.startswith("Nothing in this drawing can be drawn") for n in d.notes))
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
