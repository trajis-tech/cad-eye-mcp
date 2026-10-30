"""Drive the cad-eye MCP server over stdio, as a client would, and save every returned screen.

    runtime/python/python -I scripts/try_tools.py scenario.json out_dir

scenario.json is a list of [tool_name, {arguments}] pairs."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # drawing text is any script
    calls = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-I", "-m", "cad_eye_mcp"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, cwd=ROOT)

    def rpc(i: int, method: str, params: dict | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})}
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            m = json.loads(line)
            if m.get("id") == i:
                return m

    init = rpc(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "try_tools", "version": "0"}})
    print("server:", init["result"]["serverInfo"])
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    if calls == "list":
        for t in rpc(1, "tools/list")["result"]["tools"]:
            print(f"- {t['name']}: {t.get('description', '').splitlines()[0]}")
            print("    ", json.dumps(t["inputSchema"].get("properties", {}), ensure_ascii=False)[:300])
        return 0
    failures = 0
    for n, (name, args) in enumerate(calls, 1):
        t = time.time()
        r = rpc(n, "tools/call", {"name": name, "arguments": args})
        dt = time.time() - t
        res = r.get("result", {})
        if "error" in r or res.get("isError"):
            failures += 1
        for k, c in enumerate(res.get("content", [])):
            if c["type"] == "image":
                fn = out / f"{n:02d}_{name}.png"
                fn.write_bytes(base64.b64decode(c["data"]))
                print(f"[{n}] {name} ({dt:.1f}s): image -> {fn}")
            else:
                print(f"[{n}] {name} ({dt:.1f}s): {c.get('text', '')[:1500]}")
        if "error" in r:
            print(f"[{n}] {name}: ERROR {r['error']}")
    proc.stdin.close()
    proc.wait(timeout=30)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
