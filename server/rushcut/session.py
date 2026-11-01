"""Pick live TCP vs auto-launch vs oneshot. Same tool interface either way."""
from __future__ import annotations

import os
import socket
import time
from typing import Any

from .host_client import VegasHostClient, VegasHostError
from .host_launch import launch_vegas, run_oneshot_script, vegas_running
from .oneshot import write_oneshot_job


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Session:
    def __init__(self, client: VegasHostClient | None = None):
        self.client = client or VegasHostClient()
        self.last_mode = "none"

    def _try_ping(self) -> dict[str, Any] | None:
        try:
            result = self.client.call("ping")
        except (VegasHostError, ConnectionRefusedError, FileNotFoundError, OSError, TimeoutError):
            return None
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    def host_reachable(self) -> bool:
        cfg = self.client.config
        if cfg.transport != "tcp":
            return self._try_ping() is not None
        if not _port_open(cfg.tcp_host, cfg.tcp_port):
            return False
        return self._try_ping() is not None

    def ensure(self, project_path: str | None = None, wait_seconds: float | None = None) -> dict[str, Any]:
        """Make the host reachable. Reports which mode was used."""
        if self.host_reachable():
            self.last_mode = "live"
            info = self._try_ping() or {"ok": True}
            info = dict(info)
            info["used_mode"] = "live"
            return info

        wait = wait_seconds
        if wait is None:
            wait = float(os.environ.get("VEGAS_HOST_LAUNCH_WAIT", "45"))

        if vegas_running():
            # VEGAS is up but nothing is listening: the extension did not load in this
            # instance (installed after VEGAS started, wrong version folder, or disabled).
            # Do not spawn a second VEGAS.
            self.last_mode = "none"
            return {
                "ok": False,
                "used_mode": "none",
                "vegas_was_running": True,
                "error": (
                    "VEGAS is running but the host is not listening on 127.0.0.1:8752. "
                    "Restart VEGAS so the Application Extension loads (run host\\build.ps1 first), "
                    "or run Tools > Scripting > VegasDirectorHost."
                ),
            }

        launched = launch_vegas(project_path=project_path)
        if not launched.get("ok"):
            self.last_mode = "none"
            return {**launched, "used_mode": "none"}

        deadline = time.time() + wait
        while time.time() < deadline:
            if self.host_reachable():
                self.last_mode = launched.get("used_mode") or "launched"
                ping = self._try_ping() or {"ok": True}
                out = dict(ping)
                out["used_mode"] = self.last_mode
                out["launched"] = True
                out["vegas_was_running"] = vegas_running()
                return out
            time.sleep(0.5)

        self.last_mode = "none"
        return {
            "ok": False,
            "used_mode": "none",
            "error": (
                "Launched VEGAS but the host never bound 127.0.0.1:8752. "
                "If you skipped host\\build.ps1, run Tools > Scripting > VegasDirectorHost "
                "once, or wait for the extension to load."
            ),
            "launch": launched,
        }

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        try:
            if not self.host_reachable():
                ensured = self.ensure(project_path=params.get("project_path") or params.get("path"))
                if not ensured.get("ok"):
                    return ensured
            result = self.client.call(method, params)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            elif "ok" not in result and "error" not in result:
                result = {"ok": True, **result}
            result = dict(result)
            result.setdefault("used_mode", self.last_mode if self.last_mode != "none" else "live")
            return result
        except VegasHostError as exc:
            return {"ok": False, "error": exc.message, "code": exc.code, "used_mode": self.last_mode}
        except (ConnectionRefusedError, FileNotFoundError, OSError, TimeoutError) as exc:
            return {
                "ok": False,
                "used_mode": self.last_mode or "none",
                "error": (
                    "Could not reach the VEGAS script host. Install the Application "
                    "Extension (host\\build.ps1) or run Tools > Scripting > VegasDirectorHost. "
                    f"({exc})"
                ),
                "code": -1,
            }

    def run_oneshot(self, job: dict[str, Any]) -> dict[str, Any]:
        written = write_oneshot_job(job)
        launched = run_oneshot_script(
            written["script_path"],
            project_path=job.get("open"),
        )
        out = {**written, **launched}
        out["used_mode"] = launched.get("used_mode", "oneshot_runscript")
        self.last_mode = out["used_mode"]
        return out


_SESSION: Session | None = None


def get_session() -> Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = Session()
    return _SESSION


def reset_session(session: Session | None = None) -> Session:
    global _SESSION
    _SESSION = session if session is not None else Session()
    return _SESSION
