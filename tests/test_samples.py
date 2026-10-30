"""Regression checks on real third-party drawings (fetched with scripts/fetch_samples.py; each
check is skipped when its file is missing). Every drawing gets the same basic round (open, draw a
frame, refuse to save) plus fixed facts about it. Open times go to samples/regress-timings.json;
anything over SLOW seconds is flagged there, and over LIMIT seconds fails."""

import json
import time
import unittest
from pathlib import Path

from cad_eye_mcp import paths
from cad_eye_mcp.session import Session, ViewerError

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SLOW, LIMIT = 10.0, 90.0
TIMINGS: dict[str, dict] = {}

try:
    paths.cadcraft_exe()
    HAVE_ENGINE = True
except FileNotFoundError:
    HAVE_ENGINE = False


@unittest.skipUnless(HAVE_ENGINE, "CADCraft runtime not installed (run install.ps1 / install.sh)")
class SampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Session()

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        if TIMINGS:
            (SAMPLES / "regress-timings.json").write_text(json.dumps(TIMINGS, indent=1), encoding="utf-8")

    def open(self, rel: str):
        path = SAMPLES / rel
        if not path.exists():
            self.skipTest(f"{rel} not fetched (run scripts/fetch_samples.py)")
        t = time.perf_counter()
        d = self.s.open(str(path))
        dt = time.perf_counter() - t
        TIMINGS[rel] = {"open_s": round(dt, 2), "slow": dt > SLOW}
        self.assertLess(dt, LIMIT, f"opening {rel} took {dt:.1f}s")
        png, state = self.s.frame()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn('"view"', state)
        with self.assertRaises(ViewerError):
            self.s.command("save", {"path": "x.dxf"})
        return d

    def picked(self, p, tolerance_px=6):
        ranked = self.s.pick(p, tolerance_px)
        self.assertTrue(ranked, f"nothing picked at {p}")
        return self.s.describe([ranked[0][0]], full=True)[0]

    # -- mechanical drawings -------------------------------------------------------------------
    def test_parametric_flange_dwg(self):
        d = self.open("mech/mechanical_example-imperial.dwg")
        self.assertEqual(d.counts.get("Circle"), 8)
        self.assertEqual(len(self.s.find_type("Circle", None, None, 50)), 8)
        hole = self.picked((68.790789, 43.575))
        self.assertEqual((hole["handle"], hole["radius"]), ("368", 0.5))
        dims = self.s.describe(self.s.find_type("Dimension", None, None, 50))
        self.assertEqual(len(dims), 17)
        self.assertIn("SHAFTid=3\"", [x["text"] for x in dims])

    def test_sheet_with_tables_dwg(self):
        d = self.open("mech/blocks_and_tables_-_metric.dwg")
        self.assertIn("ISO A1", d.layouts)
        self.s.set_layout("ISO A1")
        self.assertEqual(self.s.drawing.units, "mm")
        hits = self.s.table_hits("ANDERSEN")
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0]["rows"]), 7)
        table = self.picked((348.39, 98.69))  # inside a cell of the door schedule
        self.assertEqual((table["type"], table["title"], table["rows"]), ("ACAD_TABLE", "DOOR SCHEDULE", 9))

    def test_r12_dxf_without_handles(self):
        d = self.open("mech/nist_chopper_bowl.dxf")
        self.assertNotEqual(d.work_dxf, d.source)  # ezdxf reads CADCraft's numbered copy
        dims = self.s.describe(self.s.find_type("Dimension", None, None, 100))
        self.assertGreaterEqual(len(dims), 25)
        self.assertEqual({x["type"] for x in dims}, {"DIMENSION"})

    def test_mechanical_assembly_with_proxies_and_xrefs(self):
        d = self.open("mech/DRYER_ASSEMBLY.dwg")
        self.assertEqual(d.layout, "Layout2")  # model space holds only 3D data
        self.assertGreater(d.counts.get("Polyline", 0), 1000)
        self.assertIn("ACAD_PROXY_ENTITY", d.unsupported)
        if (SAMPLES / "mech/SWITCH.dwg").exists():
            self.assertFalse([n for n in d.notes if "External references not found" in n])

    # -- entity types CADCraft does not draw -----------------------------------------------------
    def test_uncommon_types(self):
        d = self.open("dxf/ezdxf_uncommon.dxf")
        self.assertEqual(len(self.s.find_type("Proxy", None, None, 20)), d.counts.get("Proxy"))
        arc_dim = self.s.describe(self.s.find_type("ARC_DIMENSION", None, None, 5))[0]
        self.assertAlmostEqual(arc_dim["measurement"], 8.549711, places=4)  # arc length, not the angle
        self.assertTrue(d.stand_ins.get(arc_dim["handle"]), "the arc dimension must be drawn")

    def test_table_metadata(self):
        self.open("dxf/ezdxf_acad_table.dxf")
        table = self.picked((800, 130), tolerance_px=15)
        self.assertEqual((table["type"], table["rows"], table["cols"]), ("ACAD_TABLE", 10, 3))

    def test_acis_solids(self):
        d = self.open("dxf/ezdxf_3dsolids.dxf")
        self.assertEqual(d.layout, "Model")
        self.assertEqual(d.unsupported, {"3DSOLID": 1})
        self.assertEqual(sum(1 for h in d.stand_ins if d.stand_ins[h]), 4)

    # -- PCB ---------------------------------------------------------------------------------------
    def test_gerber_layer_visibility(self):
        d = self.open("gerber/clockblock")
        hidden = {l["name"] for l in d.layers if not l.get("on", True)}
        self.assertTrue({"F.Mask", "B.Mask", "F.Paste"} <= hidden)
        self.s.set_layers("isolate", ["F.Cu"])
        visible = {l["name"] for l in self.s.drawing.layers if l.get("on", True) and not l.get("frozen")}
        self.assertEqual(visible, {"F.Cu"})

    def test_large_board_over_50k_entities(self):
        d = self.open("gerber/mrfixit-v1.1.zip")
        self.assertGreater(sum(d.counts.values()), 50_000)


if __name__ == "__main__":
    unittest.main()
