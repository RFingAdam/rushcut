"""VEGAS Pro backend — the original path, unchanged.

Delegates every RPC to the existing TCP/session client so behaviour stays
bit-for-bit what main already does. Do not put VEGAS-specific workarounds
here; those belong in host_client / session / the C# host.
"""
from __future__ import annotations

from typing import Any

from .protocol import capability_sheet, fail, unsupported


class VegasBackend:
    name = "vegas"

    def __init__(self, session: Any | None = None):
        self._session = session

    def _session_obj(self) -> Any:
        if self._session is not None:
            return self._session
        from ..session import get_session

        return get_session()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method == "editor.capabilities":
            return self.capabilities()
        if method == "editor.info":
            return self.info()
        if method.startswith("marker."):
            # The C# host on main has no marker methods. Soft-fail here so a
            # future host that adds them (or PR work that must not collide
            # with #6) can take over without a protocol break.
            return unsupported(
                method,
                self.name,
                "VEGAS markers are not in the current host; use Resolve or the OTIO backend.",
            )
        result = self._session_obj().call(method, params)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("editor", self.name)
            return result
        return {"ok": True, "result": result, "editor": self.name}

    def ensure(self, project_path: str | None = None) -> dict[str, Any]:
        out = self._session_obj().ensure(project_path=project_path)
        if isinstance(out, dict):
            out = dict(out)
            out.setdefault("editor", self.name)
            return out
        return fail("ensure_host returned a non-dict", editor=self.name)

    def info(self) -> dict[str, Any]:
        host = self.call("host.info", {})
        host = dict(host) if isinstance(host, dict) else {"ok": True, "result": host}
        host["editor"] = self.name
        host.setdefault("product", "VEGAS Pro")
        return host

    def capabilities(self) -> dict[str, Any]:
        sheet = capability_sheet(
            self.name,
            live=True,
            notes="C# host inside VEGAS Pro via TCP 127.0.0.1:8752.",
            transport_play=True,
            transport_seek=True,
            motion_keyframes=True,
            fades=True,
            envelopes=True,
            titles=True,
            gain=True,
            alpha=True,
            render=True,
            markers=False,
        )
        sheet["studio_required"] = False
        sheet["platforms"] = ["windows"]
        return sheet
