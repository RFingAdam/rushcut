"""Shared editor-backend contract.

MCP tools keep their names and JSON shapes. Each backend implements the same
RPC method strings (`project.get_state`, `track.add`, …) plus extras
(`marker.*`, `editor.capabilities`). VEGAS, Resolve, FFmpeg, and OTIO all
speak this interface.

A backend is a small object with `call(method, params) -> dict`. Soft failures
are `{ok: false, error}` — never exceptions out to the MCP client.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# RPC names the VEGAS C# host implements today. New backends should cover
# these first; mark gaps in capabilities() rather than inventing new names.
HOST_METHODS = (
    "ping",
    "host.info",
    "project.get_state",
    "project.get_selected_events",
    "project.save",
    "project.new",
    "project.open",
    "project.set_video",
    "track.add",
    "track.set_composite_level",
    "track.set_props",
    "track.clear_events",
    "media.import",
    "media.place",
    "media.list",
    "media.replace",
    "media.set_alpha",
    "event.add_video",
    "event.add_audio",
    "event.add_title",
    "event.trim",
    "event.move",
    "event.delete",
    "event.set_motion",
    "event.set_fades",
    "event.set_opacity",
    "event.set_gain",
    "envelope.set_points",
    "envelope.clear",
    "transport.play",
    "transport.stop",
    "transport.seek",
    "render.list_templates",
    "render.start",
    "render.status",
)

EXTRA_METHODS = (
    "marker.add",
    "marker.list",
    "marker.delete",
    "editor.capabilities",
    "editor.info",
)


@runtime_checkable
class EditorBackend(Protocol):
    name: str

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def ensure(self, project_path: str | None = None) -> dict[str, Any]: ...

    def capabilities(self) -> dict[str, Any]: ...


def ok(**extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    out.update(extra)
    return out


def fail(error: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error}
    out.update(extra)
    return out


def refuse_discard(
    *,
    track_count: int,
    event_count: int,
    untitled: bool,
    created_by_rushcut: bool,
    project_id: str | None,
    discard_unsaved: bool,
    save_current_to: str | None,
    requested_project_id: str | None,
    host_dirty: bool = False,
) -> str | None:
    """Return an error if new/open would throw away a project with content.

    Extension edits often leave VEGAS's window-title `*` unset, so callers
    cannot trust that flag. Refuse unless this project was created by
    rushcut in the same session, or ``discard_unsaved`` is paired with the
    current ``project_id``.
    """
    if save_current_to:
        return None
    has_content = int(event_count or 0) > 0 or bool(host_dirty)
    tracks = int(track_count or 0)
    events = int(event_count or 0)
    kind = "Untitled project" if untitled else "Project"
    counts = f"tracks={tracks}, events={events}"
    if not discard_unsaved:
        if has_content:
            return (
                f"{kind} has content ({counts}). "
                "Pass save_current_to, or discard_unsaved=true with project_id."
            )
        return "Pass save_current_to (path) to save the open project first, or discard_unsaved=true."
    if not has_content:
        return None
    if created_by_rushcut:
        return None
    if requested_project_id and project_id and str(requested_project_id) == str(project_id):
        return None
    pid = f" project_id={project_id}" if project_id else " project_id"
    return (
        f"{kind} has content ({counts}). "
        "It was not created by rushcut in this session. "
        f"Pass discard_unsaved=true with{pid}, or save_current_to."
    )


def unsupported(feature: str, editor: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    msg = f"{feature} is not supported on the {editor} backend"
    if hint:
        msg += f". {hint}"
    return fail(msg, unsupported=True, editor=editor, feature=feature, **extra)


def capability_sheet(
    editor: str,
    *,
    live: bool,
    notes: str = "",
    **flags: Any,
) -> dict[str, Any]:
    """Standard capabilities payload. Values are True / False / 'partial'."""
    sheet: dict[str, Any] = {
        "ok": True,
        "editor": editor,
        "live": live,
        "notes": notes,
        "methods": {
            "project": True,
            "tracks": True,
            "media": True,
            "events": True,
            "transport_play": False,
            "transport_seek": False,
            "motion_keyframes": False,
            "fades": False,
            "envelopes": False,
            "titles": False,
            "gain": False,
            "alpha": False,
            "render": False,
            "markers": False,
        },
    }
    sheet["methods"].update(flags)
    return sheet
