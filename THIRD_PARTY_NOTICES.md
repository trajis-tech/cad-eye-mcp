# Third-party components

cad-eye-mcp's own code is MIT-licensed. The installers download these components into
`runtime/`; none of them is stored in this repository. Versions and SHA-256 checksums are pinned
in [`runtime.lock.json`](runtime.lock.json).

| Component | Used for | License |
|---|---|---|
| [CADCraft](https://github.com/storytold/cadcraft) (`cadcraft-cli`) | View engine: opens DXF/DWG, layer state, rendering | MIT OR Apache-2.0; bundled fonts under the SIL OFL (see its `ATTRIBUTION.md`) |
| [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | Private CPython interpreter | Python Software Foundation License (+ licenses of bundled libraries) |
| [Noto Sans CJK](https://github.com/notofonts/noto-cjk) | Chinese/Japanese glyphs (TC, HK, SC, JP faces) | SIL Open Font License 1.1 |
| [ezdxf](https://github.com/mozman/ezdxf) | DXF parsing, entity details, geometry | MIT |
| [gerbonara](https://gitlab.com/gerbolyze/gerbonara) | Gerber / Excellon parsing | Apache-2.0 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MCP server | MIT |
| [Pillow](https://github.com/python-pillow/Pillow) | Frame composition | MIT-CMU (HPND) |
| [NumPy](https://github.com/numpy/numpy) | Geometry math | BSD-3-Clause |

`scripts/fetch_samples.py` downloads sample drawings for testing into `samples/`. They keep
their own licenses (several are GPL or CC BY-SA) and are not part of this project; the list with
sources is written to `samples/SOURCES.tsv`.
