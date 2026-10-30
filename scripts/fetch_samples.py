"""Download third-party sample drawings into ./samples for manual and integration testing.

The files keep their own licenses (several are GPL test data), so they are fetched on demand
and never committed. Run with the private runtime:

    runtime/python/python -I scripts/fetch_samples.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "samples"

TRACESPACE = "https://raw.githubusercontent.com/tracespace/tracespace/v5/packages/fixtures/boards"
ZIRO = "https://raw.githubusercontent.com/ZiroEDA/ziro-designer/main/qa/data/pcbnew/plot/fmt_oracle"
MRFIXIT = "https://raw.githubusercontent.com/TomKeddie/prj-pcb-experiments/HEAD/mr-fixit"
LIBREDWG = "https://raw.githubusercontent.com/LibreDWG/libredwg/master/test/test-data"
CADCRAFT = "https://raw.githubusercontent.com/storytold/cadcraft/main/examples"
AUTODESK = "https://download.autodesk.com/us/samplefiles/acad"
NIST = "https://raw.githubusercontent.com/usnistgov/engineering-design-models/master/models"
EZDXF = "https://raw.githubusercontent.com/mozman/ezdxf/master"

# (target path under samples/, url, license note)
SAMPLES: list[tuple[str, str, str]] = []

for name in ["clockblock-B_Cu.gbr", "clockblock-B_Mask.gbr", "clockblock-B_SilkS.gbr",
             "clockblock-Edge_Cuts.gbr", "clockblock-F_Cu.gbr", "clockblock-F_Mask.gbr",
             "clockblock-F_Paste.gbr", "clockblock-F_SilkS.gbr", "clockblock-NPTH.drl",
             "clockblock.drl", "LICENSE"]:
    SAMPLES.append((f"gerber/clockblock/{name}", f"{TRACESPACE}/clockblock/{name}", "see LICENSE (tracespace fixture)"))
for name in ["arduino-uno.cmp", "arduino-uno.drd", "arduino-uno.gko", "arduino-uno.plc",
             "arduino-uno.sol", "arduino-uno.stc", "arduino-uno.sts", "LICENSE"]:
    SAMPLES.append((f"gerber/arduino-uno/{name}", f"{TRACESPACE}/arduino-uno/{name}", "see LICENSE (tracespace fixture)"))
for name in ["mixtape.gbl", "mixtape.gbo", "mixtape.gbp", "mixtape.gbs", "mixtape.gml",
             "mixtape.gtl", "mixtape.gto", "mixtape.gtp", "mixtape.gts", "mixtape.txt", "LICENSE"]:
    SAMPLES.append((f"gerber/8bit-mixtape/{name}", f"{TRACESPACE}/8bit-mixtape/{name}", "see LICENSE (tracespace fixture)"))

SAMPLES += [
    ("gerber/mrfixit-v1.1.zip", f"{MRFIXIT}/mrfixit-v1.1.zip", "BSD-2-Clause, TomKeddie/prj-pcb-experiments"),
    ("pcb-dxf/mrfixit.kicad_pcb.dxf", f"{MRFIXIT}/mrfixit.kicad_pcb.dxf", "BSD-2-Clause, TomKeddie/prj-pcb-experiments"),
    ("pcb-dxf/kicad_oracle_all_layers.dxf", f"{ZIRO}/dxfsingle/gerber_oracle.dxf", "GPL-3.0, ZiroEDA/ziro-designer QA data"),
    ("pcb-dxf/kicad_oracle-F_Cu.dxf", f"{ZIRO}/dxf/gerber_oracle-F_Cu.dxf", "GPL-3.0, ZiroEDA/ziro-designer QA data"),
    ("pcb-dxf/kicad_oracle-Edge_Cuts.dxf", f"{ZIRO}/dxf/gerber_oracle-Edge_Cuts.dxf", "GPL-3.0, ZiroEDA/ziro-designer QA data"),
    ("dwg/example_2000.dwg", f"{LIBREDWG}/example_2000.dwg", "GPL-3.0, LibreDWG test data"),
    ("dwg/example_2018.dwg", f"{LIBREDWG}/example_2018.dwg", "GPL-3.0, LibreDWG test data"),
    ("dwg/sample_2000.dwg", f"{LIBREDWG}/sample_2000.dwg", "GPL-3.0, LibreDWG test data"),
    ("dxf/apartment.dxf", f"{CADCRAFT}/apartment.dxf", "MIT OR Apache-2.0, storytold/cadcraft"),
    # mechanical: parametric part, a sheet with tables, an AutoCAD Mechanical assembly with xrefs
    ("mech/mechanical_example-imperial.dwg", f"{AUTODESK}/mechanical_example-imperial.dwg", "Autodesk AutoCAD sample file"),
    ("mech/blocks_and_tables_-_metric.dwg", f"{AUTODESK}/blocks_and_tables_-_metric.dwg", "Autodesk AutoCAD sample file"),
    ("mech/nist_chopper_bowl.dxf", f"{NIST}/SDRC_I-DEAS/Chopper_ms6/drafting/bowl.dxf", "NIST engineering-design-models (described there as a public-domain collection)"),
    # entity types CADCraft does not draw: arc/large radial dimensions, MESH, ACIS solids, tables
    ("dxf/ezdxf_uncommon.dxf", f"{EZDXF}/examples_dxf/uncommon.dxf", "MIT, mozman/ezdxf"),
    ("dxf/ezdxf_acad_table.dxf", f"{EZDXF}/examples_dxf/acad_table_with_blk_ref.dxf", "MIT, mozman/ezdxf"),
    ("dxf/ezdxf_3dsolids.dxf", f"{EZDXF}/examples/acistools/3dsolids.dxf", "MIT, mozman/ezdxf"),
]
for name, remote in [("DRYER_ASSEMBLY", "DRYER_ASSEMBLY"), ("CORD_GUIDE", "CORD_GUIDE"), ("DRYER_GRILL", "dryer_grill"),
                     ("FANS", "FANS"), ("HEAT_COIL", "HEAT_COIL"), ("MOTOR_BASE", "MOTOR_BASE"),
                     ("MOTOR_BRACKET", "MOTOR_BRACKET"), ("MOTOR_SHELL", "MOTOR_SHELL"), ("SWITCH", "SWITCH"),
                     ("SWITCH_SLIDE", "SWITCH_SLIDE")]:
    SAMPLES.append((f"mech/{name}.dwg", f"{NIST}/Autodesk/blowdryer/{remote}.dwg",
                    "NIST engineering-design-models (described there as a public-domain collection)"))


def main() -> int:
    failed = 0
    notes = []
    for rel, url, note in SAMPLES:
        target = DEST / rel
        notes.append(f"{rel}\t{url}\t{note}")
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                target.write_bytes(r.read())
            print(f"  ok   {rel}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {rel}: {e}")
    (DEST / "SOURCES.tsv").write_text("path\turl\tlicense\n" + "\n".join(notes) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
