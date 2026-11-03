"""Named timeline effects that wrap file-level audio/caption ops.

Each effect also works on a file without an NLE when ``path`` is passed.
On a live project, the op runs against the first matching audio (or video)
event and the media is replaced.
"""
from __future__ import annotations

from typing import Any

from .. import tools

_FILE_EFFECTS = {
    "music_bed",
    "add_music_bed",
    "bleep",
    "bleep_spans",
    "insert_audio",
    "replace_audio",
    "replace_audio_span",
    "loop_fades",
    "master",
    "master_audio",
    "cleanup",
    "audio_cleanup",
}


def _dispatch_file(effect: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    key = effect.lower()
    params = dict(params)
    if key in {"music_bed", "add_music_bed"}:
        from ..analysis.audio_ops import add_music_bed

        bed = params.pop("bed", None) or params.pop("music", None)
        if bed:
            return add_music_bed(bed, onto=path, **params)
        return add_music_bed(path, **params)
    if key in {"bleep", "bleep_spans"}:
        from ..analysis.audio_ops import bleep_spans

        return bleep_spans(path, **params)
    if key == "insert_audio":
        from ..analysis.audio_ops import insert_audio

        return insert_audio(path, **params)
    if key in {"replace_audio", "replace_audio_span"}:
        from ..analysis.audio_ops import replace_audio_span

        return replace_audio_span(path, **params)
    if key == "loop_fades":
        from ..analysis.audio_ops import loop_fades

        return loop_fades(path, **params)
    if key in {"master", "master_audio"}:
        from ..analysis.loudness import master_audio

        return master_audio(path, **params)
    if key in {"cleanup", "audio_cleanup"}:
        from ..analysis.cleanup import audio_cleanup

        return audio_cleanup(path, **params)
    return {"ok": False, "error": f"unknown apply_timeline effect {effect!r}"}


def _first_media_path(state: dict[str, Any], track_index: int | None) -> str | None:
    events = list(state.get("events") or [])
    for ev in events:
        if track_index is not None and ev.get("track_index") != track_index:
            continue
        path = ev.get("media_path") or ev.get("path")
        if path:
            return str(path)
    return None


def apply_timeline(
    effect: str,
    path: str | None = None,
    output_path: str | None = None,
    track_index: int | None = None,
    replace_media: bool = True,
    **params: Any,
) -> dict[str, Any]:
    """Apply a file-level audio op, or the same op as a timeline effect.

    Without VEGAS: pass ``path`` (and optional ``output_path``).
    With a project open: omit ``path`` and the first event on
    ``track_index`` (or any track) is processed; ``media.replace`` swaps
    the file when ``replace_media`` is true.
    """
    key = (effect or "").lower()
    if key not in _FILE_EFFECTS:
        return {"ok": False, "error": f"unknown apply_timeline effect {effect!r}"}
    used_mode = None
    event_path = path
    if not event_path:
        state = tools.get_project_state()
        used_mode = state.get("used_mode")
        if not state.get("ok"):
            return {
                "ok": False,
                "error": state.get("error") or "no open project; pass path for the file-level op",
                "used_mode": used_mode,
            }
        event_path = _first_media_path(state, track_index)
        if not event_path:
            return {"ok": False, "error": "no media on the timeline", "used_mode": used_mode}
    if output_path:
        params = dict(params)
        params["output_path"] = output_path
    result = _dispatch_file(key, event_path, params)
    result = dict(result)
    result["effect"] = key
    result["source_path"] = event_path
    if used_mode:
        result["used_mode"] = used_mode
    if (
        replace_media
        and path is None
        and result.get("ok")
        and result.get("output_path")
        and event_path
        and result["output_path"] != event_path
    ):
        swapped = tools.replace_media(event_path, str(result["output_path"]))
        result["replaced"] = swapped
    return result
