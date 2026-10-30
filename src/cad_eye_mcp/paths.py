"""Locations of the private runtime (see install.ps1 / install.sh) and the environment
used to start CADCraft without touching the user's system."""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("CAD_EYE_RUNTIME", ROOT / "runtime"))
FONT_HOME = RUNTIME / "fonthome"

# One collection covers every CJK character; the region only picks the glyph forms. Faces in
# NotoSansCJK-Regular.ttc: 0 JP, 1 KR, 2 SC, 3 TC, 4 HK.
CJK_FONT = "NotoSansCJK-Regular.ttc"
REGION_FACE = {"jp": 0, "sc": 2, "tc": 3, "hk": 4}


def font_dir() -> Path:
    """Folder that holds the bundled .otf files for this platform's per-user font layout."""
    if sys.platform == "win32":
        return FONT_HOME / "Microsoft" / "Windows" / "Fonts"
    if sys.platform == "darwin":
        return FONT_HOME / "Library" / "Fonts"
    return FONT_HOME / ".local" / "share" / "fonts"


def cjk_font() -> Path | None:
    f = font_dir() / CJK_FONT
    return f if f.is_file() else None


def cjk_region() -> str:
    region = os.environ.get("CAD_EYE_CJK", "tc").lower()
    return region if region in REGION_FACE else "tc"


def font_fallback_order() -> list[str]:
    """Families CADCraft tries for characters a drawing's font lacks, chosen region first."""
    if cjk_font() is None:
        return []
    first = cjk_region()
    return [f"Noto Sans CJK {c.upper()}" for c in [first] + [c for c in ("tc", "hk", "jp", "sc") if c != first]]


def cadcraft_exe() -> Path:
    override = os.environ.get("CAD_EYE_CADCRAFT")
    if override:
        return Path(override)
    name = "cadcraft-cli.exe" if sys.platform == "win32" else "cadcraft-cli"
    # The Linux tarball also ships share/bash-completion/completions/cadcraft-cli; skip it.
    hits = [p for p in (RUNTIME / "cadcraft").glob(f"**/{name}")
            if p.is_file() and "share" not in p.parts and (sys.platform == "win32" or os.access(p, os.X_OK))]
    if hits:
        return sorted(hits, key=lambda p: (len(p.parts), str(p)))[0]
    found = shutil.which(name)
    if found:
        return Path(found)
    raise FileNotFoundError(
        f"{name} not found under {RUNTIME / 'cadcraft'}. Run install.ps1 / install.sh first, "
        "or set CAD_EYE_CADCRAFT to the cadcraft-cli binary."
    )


def cadcraft_env() -> dict[str, str]:
    """Environment for the CADCraft process: no settings folder, and the per-user font folder
    redirected to the bundled fonts so nothing is installed system-wide."""
    env = dict(os.environ)
    env["CADCRAFT_CONFIG_DIR"] = ""
    if sys.platform == "win32":
        env["LOCALAPPDATA"] = str(FONT_HOME)
    else:
        # macOS looks in ~/Library/Fonts, Linux/BSD in ~/.local/share/fonts
        env["HOME"] = str(FONT_HOME)
        env["XDG_DATA_HOME"] = str(FONT_HOME / ".local" / "share")
    fallback = font_fallback_order()
    if fallback and "CADCRAFT_FONTFALLBACK" not in os.environ:
        env["CADCRAFT_FONTFALLBACK"] = ", ".join(fallback)
    return env


def overlay_font() -> tuple[Path, int] | None:
    """Font file and face index with CJK coverage for labels drawn on top of rendered frames."""
    f = cjk_font()
    return (f, REGION_FACE[cjk_region()]) if f else None


def cache_dir() -> Path:
    d = Path(os.environ.get("CAD_EYE_CACHE", Path(tempfile.gettempdir()) / "cad-eye-mcp"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return f"{osname}-{arch}"
