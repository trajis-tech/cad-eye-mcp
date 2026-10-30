"""A small synchronous MCP client for `cadcraft-cli mcp`, CADCraft's headless drawing session.

CADCraft is the view engine: it opens DXF/DWG, keeps layer state and renders PNGs. We talk to
it over its own MCP server (newline-delimited JSON-RPC on stdio)."""

from __future__ import annotations

import base64
import json
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import paths

PROTOCOL_VERSION = "2025-06-18"


class CadCraftError(RuntimeError):
    pass


@dataclass
class ToolResult:
    texts: list[str] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)

    def json(self) -> Any:
        """The first text block parsed as JSON (CADCraft answers tools with JSON text)."""
        if not self.texts:
            return None
        try:
            return json.loads(self.texts[0])
        except json.JSONDecodeError:
            return self.texts[0]


class CadCraft:
    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._replies: dict[int, queue.Queue] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self.starts = 0
        # called after the engine had to be restarted because it stopped or hung (not after
        # close()), to bring the new process back to the state the caller expects
        self.on_restart: Callable[[], None] | None = None
        self._closed_on_purpose = True
        self._restoring = False

    # -- process lifecycle -------------------------------------------------------------
    def _start(self) -> None:
        exe = paths.cadcraft_exe()
        self._proc = subprocess.Popen(
            [str(exe), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=paths.cadcraft_env(),
            cwd=str(paths.cache_dir()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._replies = {}
        threading.Thread(target=self._read_stdout, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cad-eye-mcp", "version": "0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.starts += 1
        self._closed_on_purpose = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self) -> None:
        if not self._alive():
            self._start()

    def close(self) -> None:
        self._closed_on_purpose = True
        self._stop()

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
            proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    # -- JSON-RPC plumbing -------------------------------------------------------------
    def _read_stdout(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            q = self._replies.get(msg.get("id"))
            if q is not None:
                q.put(msg)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
            del self._stderr_tail[:-20]

    def _send(self, msg: dict) -> None:
        self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        q: queue.Queue = queue.Queue()
        self._replies[rid] = q
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        try:
            reply = q.get(timeout=self.timeout)
        except queue.Empty:
            self._stop()
            raise CadCraftError(f"CADCraft did not answer {method} within {self.timeout:.0f}s") from None
        finally:
            self._replies.pop(rid, None)
        if "error" in reply:
            raise CadCraftError(reply["error"].get("message", str(reply["error"])))
        return reply["result"]

    # -- tools -------------------------------------------------------------------------
    def call(self, tool: str, args: dict | None = None) -> ToolResult:
        with self._lock:
            lost = not self._alive() and self.starts > 0 and not self._closed_on_purpose
            self.ensure_started()
        if lost and self.on_restart is not None and not self._restoring:
            self._restoring = True
            try:
                self.on_restart()
            finally:
                self._restoring = False
        with self._lock:
            self.ensure_started()
            try:
                result = self._request("tools/call", {"name": tool, "arguments": args or {}})
            except (BrokenPipeError, OSError) as e:
                tail = "\n".join(self._stderr_tail[-5:])
                self._stop()
                raise CadCraftError(f"CADCraft stopped: {e}\n{tail}") from e
        out = ToolResult()
        for block in result.get("content", []):
            if block.get("type") == "image":
                out.images.append(base64.b64decode(block["data"]))
            elif block.get("type") == "text":
                out.texts.append(block.get("text", ""))
        if result.get("isError"):
            raise CadCraftError(" ".join(out.texts) or f"{tool} failed")
        return out

    def execute(self, command: str, params: dict | None = None) -> Any:
        return self.call("execute", {"command": command, "params": params or {}}).json()

    def render(self, window: tuple[tuple[float, float], tuple[float, float]], width: int, height: int) -> bytes:
        (x0, y0), (x1, y1) = window
        res = self.call("render", {"window": [[x0, y0], [x1, y1]], "width": width, "height": height})
        if not res.images:
            raise CadCraftError("render returned no image: " + " ".join(res.texts))
        return res.images[0]
