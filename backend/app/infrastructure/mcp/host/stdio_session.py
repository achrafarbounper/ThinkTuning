"""Small dependency-free JSON-RPC stdio transport for MCP 2025-06-18."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from typing import Any

logger = logging.getLogger("thinktuning.mcp.host.stdio")


class StdioSessionError(RuntimeError):
    """A process or protocol failure."""


class StdioSession:
    def __init__(
        self, command: list[str], env: dict[str, str] | None = None, timeout: float = 10.0
    ) -> None:
        if not command:
            raise ValueError("MCP command cannot be empty")
        self.command, self.env, self.timeout = command, env, timeout
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._lock = threading.RLock()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=self.env,
                bufsize=1,
            )
        except OSError as exc:
            raise StdioSessionError(f"cannot start MCP server: {self.command[0]}") from exc
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "thinktuning-host", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if not self.process or self.process.poll() is not None:
                raise StdioSessionError("MCP server is not running")
            assert self.process.stdin and self.process.stdout
            request_id = self._next_id
            self._next_id += 1
            self.process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
                + "\n"
            )
            self.process.stdin.flush()
            result: dict[str, Any] | None = [None]
            error: list[BaseException] = []

            def read() -> None:
                try:
                    line = self.process.stdout.readline()
                    result[0] = json.loads(line) if line else None
                except (OSError, ValueError) as exc:
                    error.append(exc)

            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            thread.join(self.timeout)
            if thread.is_alive():
                raise StdioSessionError(f"MCP request timeout: {method}")
            if error or not result[0]:
                raise StdioSessionError(f"invalid MCP response: {method}")
            response = result[0]
            if "error" in response:
                raise StdioSessionError(str(response["error"]))
            return response.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.process or self.process.poll() is not None:
            raise StdioSessionError("MCP server is not running")
        assert self.process.stdin
        self.process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                }
            )
            + "\n"
        )
        self.process.stdin.flush()

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
