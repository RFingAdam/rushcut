"""Client for the Rushcut MCP <-> VEGAS script host RPC channel.

Two transports are supported:
  - "tcp": loopback TCP (default). The current host listens here.
  - "pipe": Windows named pipe. Client path exists; the host does not listen.

Extension-mode hosts require a token (VEGAS_HOST_TOKEN or the runtime file
under %LOCALAPPDATA%\\vegas-director-mcp\\token). Script-menu mode does not.
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import Any


LONG_METHODS = {"project.save", "project.new", "project.open", "render.start"}


def _token_file_path() -> str:
    local_app = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or ""
    if local_app:
        return os.path.join(local_app, "vegas-director-mcp", "token")
    return os.path.join(os.path.expanduser("~"), "AppData", "Local", "vegas-director-mcp", "token")


def _load_host_token() -> str | None:
    """Read the runtime token written by the extension host.

    Script-menu mode does not require a token. Extension mode does. The file
    lives under %LOCALAPPDATA%\\vegas-director-mcp\\token and is never shipped
    in the repo. Override with VEGAS_HOST_TOKEN.
    """
    env = os.environ.get("VEGAS_HOST_TOKEN", "").strip()
    if env:
        return env
    path = _token_file_path()
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
        return token or None
    except OSError:
        return None


class VegasHostError(RuntimeError):
    """Raised when the script host returns a JSON-RPC error object."""

    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class VegasHostConfig:
    transport: str = "pipe"
    pipe_name: str = "vegas-director"
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 8752

    @classmethod
    def from_env(cls) -> "VegasHostConfig":
        return cls(
            transport=os.environ.get("VEGAS_HOST_TRANSPORT", "tcp"),
            pipe_name=os.environ.get("VEGAS_HOST_PIPE_NAME", "vegas-director"),
            tcp_host=os.environ.get("VEGAS_HOST_ADDRESS", "127.0.0.1"),
            tcp_port=int(os.environ.get("VEGAS_HOST_PORT", "8752")),
        )


class VegasHostClient:
    """Blocking, one-request-per-connection JSON-RPC client.

    Kept deliberately simple for the Phase 0/1 scaffold: opens a fresh
    connection per call. Once real usage patterns emerge (e.g. many calls in
    a tight edit loop), switch to a persistent connection with a
    request-id-keyed response dispatcher rather than prematurely optimizing
    now.
    """

    def __init__(self, config: VegasHostConfig | None = None):
        self.config = config or VegasHostConfig.from_env()
        self._next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req_id = self._next_id
        self._next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        token = _load_host_token()
        if token:
            request["token"] = token
        line = json.dumps(request) + "\n"

        self._timeout = 1800 if method in LONG_METHODS else float(os.environ.get("VEGAS_HOST_TIMEOUT", "60"))
        raw_response = self._send(line)
        response = json.loads(raw_response.lstrip("\ufeff"))

        if "error" in response:
            err = response["error"]
            raise VegasHostError(err.get("code", -32000), err.get("message", "unknown error"))
        return response.get("result")

    def _send(self, line: str) -> str:
        if self.config.transport == "tcp":
            return self._send_tcp(line)
        if self.config.transport == "pipe":
            return self._send_pipe(line)
        raise ValueError(f"Unknown transport: {self.config.transport!r}")

    def _send_tcp(self, line: str) -> str:
        with socket.create_connection(
            (self.config.tcp_host, self.config.tcp_port), timeout=getattr(self, "_timeout", 60)
        ) as sock:
            sock.sendall(line.encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8-sig").strip()

    def _send_pipe(self, line: str) -> str:
        # Named pipes are Windows-only and use a distinct API from sockets.
        # win32file is provided by pywin32 -- imported lazily so this module
        # still imports cleanly on non-Windows dev machines (e.g. for
        # running the media-analysis tools' unit tests without a Windows
        # box), and only fails at call time if pipe transport is actually
        # requested on a platform that can't support it.
        try:
            import win32file  # type: ignore
            import win32pipe  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Named-pipe transport requires pywin32 and a Windows host. "
                "Install pywin32, or set VEGAS_HOST_TRANSPORT=tcp instead."
            ) from exc

        pipe_path = rf"\\.\pipe\{self.config.pipe_name}"
        handle = win32file.CreateFile(  # type: ignore[misc]
            pipe_path,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
        try:
            win32file.WriteFile(handle, line.encode("utf-8"))  # type: ignore[arg-type]
            _, data = win32file.ReadFile(handle, 65536)  # type: ignore[arg-type]
            data_bytes: bytes = data if isinstance(data, bytes) else data.encode("utf-8")
            return data_bytes.decode("utf-8-sig").strip()
        finally:
            handle.Close()

