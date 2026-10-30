"""Unit tests that need no CADCraft: view math, Gerber conversion, CJK region guessing."""

import math
import tempfile
import unittest
from pathlib import Path

import ezdxf

from cad_eye_mcp import deep, gerber, proxy
from cad_eye_mcp.frame import nice_step
from cad_eye_mcp.session import guess_cjk_region
from cad_eye_mcp.view import RULER_LEFT, RULER_TOP, View, fit_view

GERBER = """%FSLAX26Y26*%
%MOMM*%
%ADD10C,0.5*%
%ADD11R,1.0X2.0*%
D10*
X0Y0D02*
X10000000Y0D01*
D11*
X5000000Y5000000D03*
G36*
X0Y10000000D02*
X10000000Y10000000D01*
X10000000Y12000000D01*
X0Y12000000D01*
X0Y10000000D01*
G37*
M02*
"""


class ViewTest(unittest.TestCase):
    def test_round_trip(self):
        v = View(50, 25, 100, 1000, 500)
        for wx, wy in ((0, 0), (50, 25), (149.5, -24)):
            px, py = v.world_to_frame(wx, wy)
            x, y = v.frame_to_world(px, py)
            self.assertAlmostEqual(x, wx)
            self.assertAlmostEqual(y, wy)

    def test_frame_origin_is_window_top_left(self):
        v = View(0, 0, 10, 200, 100)
        (x0, _), (_, y1) = v.window
        self.assertEqual(v.world_to_frame(x0, y1), (RULER_LEFT, RULER_TOP))

    def test_zoom_keeps_anchor_on_screen(self):
        v = View(0, 0, 100, 800, 600)
        anchor = (30.0, -10.0)
        before = v.world_to_area(*anchor)
        after = v.zoom(4, anchor).world_to_area(*anchor)
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])

    def test_fit_contains_box(self):
        v = fit_view(((10, 10), (110, 20)), 400, 300)
        (x0, y0), (x1, y1) = v.window
        self.assertTrue(x0 <= 10 and x1 >= 110 and y0 <= 10 and y1 >= 20)

    def test_nice_step(self):
        self.assertEqual(nice_step(0.13), 0.2)
        self.assertEqual(nice_step(3), 5)
        self.assertEqual(nice_step(70), 100)


class GerberTest(unittest.TestCase):
    def test_convert_single_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "board-F_Cu.gbr"
            src.write_text(GERBER)
            out = Path(tmp) / "out.dxf"
            self.assertTrue(gerber.looks_like_gerber(src))
            summary = gerber.convert(src, out)
            doc = ezdxf.readfile(out)
            self.assertEqual(doc.header["$INSUNITS"], 4)
            kinds = sorted(e.dxftype() for e in doc.modelspace())
            # track (wide polyline) + 2 round caps, one rectangular flash, one region
            self.assertEqual(kinds.count("SOLID"), 1)
            self.assertEqual(kinds.count("HATCH"), 1)
            self.assertGreaterEqual(kinds.count("LWPOLYLINE"), 3)
            hatch = next(iter(doc.modelspace().query("HATCH")))
            self.assertEqual(hatch.dxf.color, 256, "regions must follow the layer colour")
            ext = deep.extents(doc.modelspace())
            self.assertAlmostEqual(ext[1][0], 10.25, places=3)  # track end + half the 0.5 mm width
            self.assertEqual(summary["units"], "mm")

    def test_bulge_full_circle_is_split(self):
        verts = gerber._bulge_vertices([((1, 0), (1, 0), (False, (0, 0)))])
        self.assertEqual(len(verts), 2)
        self.assertTrue(all(abs(b) <= 1.0 + 1e-9 for *_, b in verts))


class DeepTest(unittest.TestCase):
    def test_distance_and_measure(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        line = msp.add_line((0, 0), (10, 0))
        circle = msp.add_circle((0, 0), 2)
        self.assertAlmostEqual(deep.distance(line, (5, 3)), 3, places=6)
        self.assertAlmostEqual(deep.measure_entity(circle)["area"], math.pi * 4, places=4)
        self.assertEqual(deep.summary(line)["length"], 10)

    def test_tilted_arc_reports_drawing_coordinates(self):
        doc = ezdxf.new()
        arc = doc.modelspace().add_arc((5, 0), 1, 0, 90, dxfattribs={"extrusion": (0, 0, -1)})
        self.assertEqual(deep.summary(arc)["center"], [-5.0, 0.0])

    def test_mtext_box_wraps_and_follows_inline_height(self):
        doc = ezdxf.new()
        words = " ".join(["WORD"] * 40)
        m = doc.modelspace().add_mtext(f"\\H2x;TITLE\\P\\H0.5x;{words}",
                                       dxfattribs={"char_height": 10, "width": 200})
        box = deep.text_box(m)
        width = box[:, 0].max() - box[:, 0].min()
        height = box[:, 1].max() - box[:, 1].min()
        self.assertAlmostEqual(width, 200)
        # 20 high title + 40 words at 10 high wrapped into several lines
        self.assertGreater(height, 20 + 3 * 10 * 5 / 3)

    def test_no_proxies_in_plain_dxf(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.dxf"
            ezdxf.new().saveas(path)
            self.assertFalse(proxy.needs_explode(path))


class CjkTest(unittest.TestCase):
    def test_regions(self):
        self.assertEqual(guess_cjk_region(["客廳 臺北市 這邊 說明"]), "tc")
        self.assertEqual(guess_cjk_region(["简体中文 电路板 设计说明 们"]), "sc")
        self.assertEqual(guess_cjk_region(["ひらがな カタカナ 図面"]), "jp")
        self.assertEqual(guess_cjk_region(["嘅 㗎 啲 喺 冇 嘢"]), "hk")
        self.assertIsNone(guess_cjk_region(["LIVING ROOM 12 m2"]))


if __name__ == "__main__":
    unittest.main()
