"""Generate the small committed test drawings in this folder (run with the private runtime):

    runtime/python/python -I tests/fixtures/make_fixtures.py
"""

from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

HERE = Path(__file__).parent


def cjk_mixed() -> Path:
    """A floor-plan-like sheet with Traditional/HK/Simplified Chinese, Japanese and English text,
    styles that point at fonts most machines lack (SHX big fonts, mingliu, simsun, msgothic),
    dimensions, a title block with attributes, hatches and wide polylines."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name, color in (("牆壁", 7), ("標註", 3), ("文字-台灣", 2), ("文字-香港", 4), ("文字-简体", 6),
                        ("文字-日本語", 5), ("圖框", 8), ("填充", 9)):
        doc.layers.add(name, color=color)
    doc.styles.add("TW_MING", font="mingliu.ttc")
    doc.styles.add("CN_SONG", font="simsun.ttc")
    doc.styles.add("JP_GOTHIC", font="msgothic.ttc")
    shx = doc.styles.add("SHX_BIG", font="txt.shx")
    shx.dxf.bigfont = "chineset.shx"
    msp = doc.modelspace()

    # walls: two rooms, 10 x 6 m and 6 x 6 m, 200 mm walls as wide polylines
    walls = {"layer": "牆壁", "const_width": 200}
    msp.add_lwpolyline([(0, 0), (16000, 0), (16000, 6000), (0, 6000)], close=True, dxfattribs=walls)
    msp.add_lwpolyline([(10000, 0), (10000, 6000)], dxfattribs=walls)
    door = msp.add_arc((10000, 2000), 900, 90, 180, dxfattribs={"layer": "牆壁", "color": 1})
    msp.add_line((10000, 2000), (10000, 2900), dxfattribs={"layer": "牆壁", "color": 1})
    hatch = msp.add_hatch(color=256, dxfattribs={"layer": "填充"})
    hatch.set_pattern_fill("ANSI31", scale=40)
    hatch.paths.add_polyline_path([(10100, 100), (15900, 100), (15900, 1500), (10100, 1500)], is_closed=True)

    texts = [
        ("文字-台灣", "TW_MING", (1000, 4800), "客廳 臺北市信義區 骨 角 令 遍 這"),
        ("文字-香港", "TW_MING", (1000, 4000), "香港 澳門 茶餐廳 嘅 㗎 啲 喺 冇"),
        ("文字-简体", "CN_SONG", (1000, 3200), "简体中文 电路板 设计说明 门 们"),
        ("文字-日本語", "JP_GOTHIC", (1000, 2400), "日本語 ひらがな カタカナ 図面 寸法"),
        ("文字-台灣", "SHX_BIG", (1000, 1600), "SHX 大字體 臥室 Bedroom Ø25 ±0.1 45°"),
    ]
    for layer, style, pos, s in texts:
        msp.add_text(s, height=300, dxfattribs={"layer": layer, "style": style}).set_placement(
            pos, align=TextEntityAlignment.LEFT)
    msp.add_mtext("臥室 Bedroom\\P面積 36.0 m²", dxfattribs={"layer": "文字-台灣", "char_height": 350,
                                                         "style": "TW_MING"}).set_location((11500, 4500))

    # mm drawing: EZDXF's dimstyle assumes metres (dimlfac=100), so reset scale and sizes
    big = {"dimtxt": 250, "dimasz": 200, "dimexe": 100, "dimexo": 100, "dimgap": 60, "dimlfac": 1}
    dim = msp.add_linear_dim(base=(0, -800), p1=(0, 0), p2=(16000, 0), dimstyle="EZDXF",
                             override=big, dxfattribs={"layer": "標註"})
    dim.render()
    dim = msp.add_linear_dim(base=(-800, 0), p1=(0, 0), p2=(0, 6000), angle=90, dimstyle="EZDXF",
                             override=big, dxfattribs={"layer": "標註"})
    dim.render()

    # title block with attributes
    tb = doc.blocks.new("圖框_A3")
    tb.add_lwpolyline([(0, 0), (6000, 0), (6000, 1500), (0, 1500)], close=True)
    tb.add_line((0, 750), (6000, 750))
    for tag, prompt, y in (("圖名", "Drawing title", 1000), ("圖號", "Drawing no.", 250)):
        tb.add_attdef(tag, (200, y), dxfattribs={"height": 280, "prompt": prompt, "style": "TW_MING"})
    ref = msp.add_blockref("圖框_A3", (10000, -2500), dxfattribs={"layer": "圖框"})
    ref.add_auto_attribs({"圖名": "一樓平面圖 First floor plan", "圖號": "A-101"})

    out = HERE / "cjk_mixed.dxf"
    doc.saveas(out)
    return out


def sheet_layout() -> Path:
    """A part in model space placed on an A3 sheet through a 1:1 viewport and a 1:2 detail
    viewport, with a border, a title text and notes in paper space."""
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (120, 0), (120, 60), (0, 60)], close=True)
    msp.add_circle((30, 30), 10)
    msp.add_circle((90, 30), 10)
    sheet = doc.layouts.new("A3 圖紙")
    sheet.page_setup(size=(420, 297), margins=(0, 0, 0, 0), units="mm")
    sheet.add_lwpolyline([(10, 10), (410, 10), (410, 287), (10, 287)], close=True)
    sheet.add_viewport(center=(160, 160), size=(200, 120), view_center_point=(60, 30), view_height=120)
    # a detail view at half scale: one paper mm shows two model mm
    sheet.add_viewport(center=(300, 220), size=(100, 60), view_center_point=(60, 30), view_height=120)
    sheet.add_text("圖號 M-001 法蘭", height=6).set_placement((300, 30), align=TextEntityAlignment.LEFT)
    sheet.add_mtext("NOTES: ALL HOLES THROUGH.\\PBREAK SHARP EDGES.",
                    dxfattribs={"char_height": 4}).set_location((300, 120))
    out = HERE / "sheet_layout.dxf"
    doc.saveas(out)
    return out


def solids() -> Path:
    """A 3DSOLID cube with real ACIS data next to a 3DSOLID whose ACIS data is unreadable."""
    from ezdxf.acis import api as acis
    from ezdxf.render import forms

    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    cube = msp.add_3dsolid(dxfattribs={"layer": "實體"})
    acis.export_dxf(cube, [acis.body_from_mesh(forms.cube().scale_uniform(20))])
    broken = msp.add_3dsolid(dxfattribs={"layer": "實體"})
    broken.sab = b"ACIS BinaryFile but not really"
    out = HERE / "solids.dxf"
    doc.saveas(out)
    return out


if __name__ == "__main__":
    print(cjk_mixed())
    print(sheet_layout())
    print(solids())
