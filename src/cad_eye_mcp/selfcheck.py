"""`python -m cad_eye_mcp --check`: verify the runtime without starting the MCP server."""

from __future__ import annotations

import subprocess
import sys

from . import __version__, paths


def run() -> int:
    ok = True

    def report(name: str, good: bool, detail: str) -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else 'FAIL'}] {name}: {detail}")

    print(f"cad-eye-mcp {__version__} on {paths.platform_key()}, Python {sys.version.split()[0]}")
    for mod in ("mcp", "ezdxf", "gerbonara", "PIL", "numpy"):
        try:
            m = __import__(mod)
            report(mod, True, getattr(m, "__version__", "imported"))
        except Exception as e:  # noqa: BLE001
            report(mod, False, str(e))
    try:
        exe = paths.cadcraft_exe()
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                             env=paths.cadcraft_env(), timeout=60)
        report("cadcraft", out.returncode == 0, out.stdout.strip() or out.stderr.strip())
    except Exception as e:  # noqa: BLE001
        report("cadcraft", False, str(e))
    font = paths.cjk_font()
    report("fonts", font is not None, font.name if font else f"{paths.CJK_FONT} missing")
    return 0 if ok else 1
