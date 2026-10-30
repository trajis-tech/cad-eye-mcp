"""Second stage of install.sh, run by the freshly unpacked private Python: install the Python
packages, CADCraft and the CJK fonts into ./runtime, verifying every download's SHA-256."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
DOWNLOADS = RUNTIME / "downloads"
sys.path.insert(0, str(ROOT / "src"))

from cad_eye_mcp import paths  # noqa: E402


def fetch(url: str, sha256: str, name: str) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / name
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
        return dest
    print(f"    downloading {name}", flush=True)
    with urllib.request.urlopen(url, timeout=300) as r:
        data = r.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise SystemExit(f"SHA-256 mismatch for {name}\n  expected {sha256}\n  actual   {actual}")
    dest.write_bytes(data)
    return dest


def main() -> int:
    force = "--force" in sys.argv
    lock = json.loads((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))
    platform = paths.platform_key()

    print("==> Python packages", flush=True)
    subprocess.check_call([sys.executable, "-I", "-m", "pip", "install", "--isolated", "--quiet",
                           "--disable-pip-version-check", "--no-warn-script-location",
                           "-r", str(ROOT / "requirements.txt")])
    import sysconfig

    Path(sysconfig.get_paths()["purelib"], "cad_eye_mcp_src.pth").write_text(str(ROOT / "src") + "\n",
                                                                               encoding="utf-8")

    cc = lock["cadcraft"]
    target = RUNTIME / "cadcraft" / cc["version"]
    if force or not (target / ".complete").exists():
        print(f"==> CADCraft {cc['version']} ({platform})", flush=True)
        asset = cc["assets"][platform]
        archive = fetch(asset["url"], asset["sha256"], asset["url"].rsplit("/", 1)[-1])
        target.mkdir(parents=True, exist_ok=True)
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
        else:
            with tarfile.open(archive) as t:
                t.extractall(target, filter="tar")
        for exe in target.rglob("cadcraft-cli"):
            if "share" not in exe.parts:  # not the bash completion script of the same name
                exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (target / ".complete").touch()

    print("==> Fonts", flush=True)
    font_dir = paths.font_dir()
    font_dir.mkdir(parents=True, exist_ok=True)
    for f in lock["fonts"]["files"]:
        dest = font_dir / f["name"]
        if force or not dest.exists():
            dest.write_bytes(fetch(f["url"], f["sha256"], f["name"]).read_bytes())

    print("==> Checking the installation", flush=True)
    return subprocess.call([sys.executable, "-I", "-m", "cad_eye_mcp", "--check"], env=dict(os.environ))


if __name__ == "__main__":
    sys.exit(main())
