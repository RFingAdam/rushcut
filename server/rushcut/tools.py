"""MCP tool definitions backed by the current editor backend."""
from __future__ import annotations

import os
import time
from typing import Any

from .output_policy import check_output_path
from .render.atomic import final_path_from_partial, partial_path, promote_partial
from .session import get_session

_RENDER_JOBS: dict[str, dict[str, Any]] = {}
_COMPLETE = {"complete", "completed", "done", "success", "finished"}
_FAILED = {"failed", "fail", "error", "canceled", "cancelled"}
_INTERRUPTED = {"interrupted", "interrupt", "aborted"}


def _call(method: str, params: dict[str, Any] | None = None) -> dict:
    from .backends import get_backend

    return get_backend().call(method, params)


def reset_render_jobs() -> None:
    """Test helper."""
    _RENDER_JOBS.clear()


def _remember_job(job_id: str | None, final_path: str, partial: str) -> None:
    if not job_id:
        return
    _RENDER_JOBS[str(job_id)] = {
        "final_path": final_path,
        "partial_path": partial,
        "promoted": False,
        "started_at": time.time(),
    }


def _this_job_wrote(path: str, started_at: float) -> bool:
    try:
        return os.path.getmtime(path) + 0.05 >= started_at
    except OSError:
        return False


def _promote_job(job_id: str | None, status: dict[str, Any]) -> dict[str, Any]:
    key = str(job_id or status.get("job_id") or "")
    info = _RENDER_JOBS.get(key) if key else None
    if info is None:
        reported = status.get("partial_path") or status.get("output_path")
        if reported:
            reported_s = str(reported)
            info = {
                "final_path": final_path_from_partial(reported_s),
                "partial_path": partial_path(final_path_from_partial(reported_s)),
                "promoted": False,
                "started_at": 0.0,
            }
            if key:
                _RENDER_JOBS[key] = info
    if not info or info.get("promoted"):
        return status
    final = info["final_path"]
    partial = info["partial_path"]
    started_at = float(info.get("started_at") or 0.0)
    if not os.path.isfile(partial) and os.path.isfile(final) and _this_job_wrote(final, started_at):
        # Host ignored the .partial name and wrote the deliverable path.
        try:
            os.replace(final, partial)
        except OSError as exc:
            out = dict(status)
            out["ok"] = False
            out["error"] = f"could not park render for QA: {exc}"
            return out
    promoted = promote_partial(partial, final)
    info["promoted"] = bool(promoted.get("ok"))
    out = dict(status)
    out.update(promoted)
    if promoted.get("ok"):
        out["ok"] = True
        out.setdefault("state", "complete")
    else:
        out["ok"] = False
        out.setdefault("state", "failed")
    return out


def ping() -> dict:
    return _call("ping")


def host_info() -> dict:
    """Host version, mode (script vs extension), and request count."""
    return _call("host.info")


def get_project_state() -> dict:
    return _call("project.get_state")


def get_selected_events() -> dict:
    """List currently selected timeline events (track_index / event_index)."""
    return _call("project.get_selected_events")


def save_project(path: str | None = None) -> dict:
    params: dict[str, Any] = {}
    if path:
        params["path"] = path
    return _call("project.save", params)


def add_track(type: str = "video", name: str = "", index: int | None = None) -> dict:
    return _call("track.add", {"type": type, "name": name, **({"index": index} if index is not None else {})})


def set_track_composite_level(track_index: int, level: float) -> dict:
    """Set VideoTrack.CompositeLevel (0..1 opacity for the whole track)."""
    return _call(
        "track.set_composite_level",
        {"track_index": track_index, "level": level},
    )


def import_media(path: str) -> dict:
    return _call("media.import", {"path": path})


def place_media(
    path: str,
    track_index: int,
    start_seconds: float = 0.0,
    length_seconds: float = -1.0,
) -> dict:
    """Place media on a track; host picks video vs audio from track type."""
    return _call(
        "media.place",
        {
            "path": path,
            "track_index": track_index,
            "start_seconds": start_seconds,
            "length_seconds": length_seconds,
        },
    )


def add_video_event(
    track_index: int,
    media_path: str,
    start_seconds: float = 0.0,
    length_seconds: float = -1.0,
) -> dict:
    return _call(
        "event.add_video",
        {
            "track_index": track_index,
            "media_path": media_path,
            "start_seconds": start_seconds,
            "length_seconds": length_seconds,
        },
    )


def add_audio_event(
    track_index: int,
    media_path: str,
    start_seconds: float = 0.0,
    length_seconds: float = -1.0,
) -> dict:
    return _call(
        "event.add_audio",
        {
            "track_index": track_index,
            "media_path": media_path,
            "start_seconds": start_seconds,
            "length_seconds": length_seconds,
        },
    )


def add_title(
    track_index: int,
    start_seconds: float,
    length_seconds: float,
    text: str,
    preset: str | None = "(Default)",
) -> dict:
    """Add a native Titles & Text generator event and set OFX Text (RTF)."""
    params: dict[str, Any] = {
        "track_index": track_index,
        "start_seconds": start_seconds,
        "length_seconds": length_seconds,
        "text": text,
    }
    if preset is not None:
        params["preset"] = preset
    return _call("event.add_title", params)


def trim_event(
    track_index: int,
    event_index: int,
    start_seconds: float | None = None,
    length_seconds: float | None = None,
    take_offset_seconds: float | None = None,
) -> dict:
    params: dict[str, Any] = {
        "track_index": track_index,
        "event_index": event_index,
    }
    if start_seconds is not None:
        params["start_seconds"] = start_seconds
    if length_seconds is not None:
        params["length_seconds"] = length_seconds
    if take_offset_seconds is not None:
        params["take_offset_seconds"] = take_offset_seconds
    return _call("event.trim", params)


def move_event(track_index: int, event_index: int, start_seconds: float) -> dict:
    return _call(
        "event.move",
        {
            "track_index": track_index,
            "event_index": event_index,
            "start_seconds": start_seconds,
        },
    )


def delete_event(track_index: int, event_index: int) -> dict:
    return _call(
        "event.delete",
        {"track_index": track_index, "event_index": event_index},
    )


def set_motion_keyframes(
    track_index: int,
    event_index: int,
    keyframes: list[dict[str, Any]],
    reset: bool = True,
) -> dict:
    """Set VideoMotion pan/crop keyframes.

    Each keyframe dict: {at_seconds, scale, pan_x, pan_y}
    scale 1.0 = no zoom; 1.4 ~= 40% zoom-in. pan_* in -1..1 (neg = left/up).
    """
    return _call(
        "event.set_motion",
        {
            "track_index": track_index,
            "event_index": event_index,
            "reset": reset,
            "keyframes": keyframes,
        },
    )


def set_event_fades(
    track_index: int,
    event_index: int,
    fade_in_seconds: float | None = None,
    fade_out_seconds: float | None = None,
    dissolve: bool = False,
    curve: str = "smooth",
    reciprocal_curve: str | None = None,
) -> dict:
    """Set FadeIn/FadeOut Length + Curve; optional Dissolve transition."""
    params: dict[str, Any] = {
        "track_index": track_index,
        "event_index": event_index,
        "dissolve": dissolve,
        "curve": curve,
    }
    if fade_in_seconds is not None:
        params["fade_in_seconds"] = fade_in_seconds
    if fade_out_seconds is not None:
        params["fade_out_seconds"] = fade_out_seconds
    if reciprocal_curve is not None:
        params["reciprocal_curve"] = reciprocal_curve
    return _call("event.set_fades", params)


def set_event_opacity(track_index: int, event_index: int, opacity: float) -> dict:
    """Set VideoEvent opacity via FadeIn.Gain (0..1)."""
    return _call(
        "event.set_opacity",
        {
            "track_index": track_index,
            "event_index": event_index,
            "opacity": opacity,
        },
    )


def set_envelope_points(
    track_index: int,
    envelope_type: str,
    points: list[dict[str, Any]],
) -> dict:
    """Create/update a track envelope and set points.

    envelope_type: Volume (audio) or CompositeLevel (video).
    points: [{at_seconds, value, curve}]
    """
    return _call(
        "envelope.set_points",
        {
            "track_index": track_index,
            "envelope_type": envelope_type,
            "points": points,
        },
    )


def render_start(
    output_path: str | None = None,
    template_name: str | None = None,
    renderer_name: str | None = None,
    start_seconds: float | None = None,
    length_seconds: float | None = None,
    async_render: bool = True,
) -> dict:
    """Start a render. Default is async (Vegas.BeginRender) so the UI marshal returns.

    The backend is asked to write ``<name>.partial.mp4``. The file is
    renamed to *output_path* only after ``wait_for_render`` (or a
    blocking start) verifies moov + a full decode.
    """
    params: dict[str, Any] = {"async": async_render}
    final_path = output_path
    partial = None
    if output_path is not None:
        refused = check_output_path(output_path)
        if refused:
            return refused
        final_path = output_path
        partial = partial_path(output_path)
        params["output_path"] = partial
        params["final_path"] = final_path
    if template_name is not None:
        params["template_name"] = template_name
    if renderer_name is not None:
        params["renderer_name"] = renderer_name
    if start_seconds is not None:
        params["start_seconds"] = start_seconds
    if length_seconds is not None:
        params["length_seconds"] = length_seconds
    result = _call("render.start", params)
    if isinstance(result, dict):
        result = dict(result)
        if final_path:
            result.setdefault("output_path", final_path)
            result["partial_path"] = partial
        job_id = result.get("job_id")
        if job_id and final_path and partial:
            _remember_job(str(job_id), final_path, partial)
        state = str(result.get("state") or "").lower()
        if (
            final_path
            and partial
            and result.get("ok")
            and not async_render
            and state in _COMPLETE
        ):
            result = _promote_job(result.get("job_id"), result)
    return result


def render_status(job_id: str | None = None) -> dict:
    params: dict[str, Any] = {}
    if job_id:
        params["job_id"] = job_id
    result = _call("render.status", params)
    if not isinstance(result, dict):
        return result
    result = dict(result)
    state = str(result.get("state") or result.get("status") or "").lower()
    if state in _INTERRUPTED:
        result["ok"] = False
        result["state"] = "interrupted"
        result.setdefault("error", "render interrupted")
    info = _RENDER_JOBS.get(str(job_id or result.get("job_id") or ""))
    if info:
        result.setdefault("output_path", info["final_path"])
        result.setdefault("partial_path", info["partial_path"])
    return result


def wait_for_render(
    job_id: str | None = None,
    timeout_seconds: float = 1800,
    poll_seconds: float = 1.5,
) -> dict:
    """Poll render.status until complete/failed/interrupted or timeout.

    On success the ``.partial`` file is decode-checked and renamed.
    """
    deadline = time.time() + timeout_seconds
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = render_status(job_id)
        if not last.get("ok") and last.get("code") == -32601:
            return last
        state = str(last.get("state") or last.get("status") or "").lower()
        if state in _COMPLETE:
            return _promote_job(job_id or last.get("job_id"), last)
        if state in _INTERRUPTED:
            last = dict(last)
            last["ok"] = False
            last["state"] = "interrupted"
            last.setdefault("error", "render interrupted")
            return last
        if state in _FAILED:
            last = dict(last)
            last["ok"] = False
            last.setdefault("error", f"render {state}")
            return last
        time.sleep(poll_seconds)
    last = dict(last or {})
    last["ok"] = False
    last["error"] = f"render still running after {timeout_seconds}s"
    return last


def set_project_video(
    width: int | None = None,
    height: int | None = None,
    frame_rate: float | None = None,
) -> dict:
    params: dict[str, Any] = {}
    if width is not None:
        params["width"] = width
    if height is not None:
        params["height"] = height
    if frame_rate is not None:
        params["frame_rate"] = frame_rate
    return _call("project.set_video", params)


def ensure_host(project_path: str | None = None) -> dict:
    """Reach the current editor (FFmpeg, VEGAS host, Resolve Studio, or OTIO store)."""
    from .backends import get_backend

    return get_backend().ensure(project_path)


def run_oneshot(job: dict[str, Any]) -> dict:
    """Generate a .cs job and /RUNSCRIPT it. VEGAS GUI still opens (no headless mode)."""
    return get_session().run_oneshot(job)


def _list_all_render_templates(
    renderer_contains: str = "",
    template_contains: str = "",
    page_size: int = 500,
) -> dict:
    """Collect every matching VEGAS render template.

    The host used to cap at 200/400 and MAGIX AVC/AAC MP4 templates sat
    after that. New host: ``limit=0`` is unlimited. Old host treated
    ``limit=0`` as empty, so we retry with a high cap and paginate when
    ``truncated`` is set. ``offset`` is ignored by old hosts — stop if a
    later page repeats the first item.
    """
    templates: list[dict[str, Any]] = []
    offset = 0
    last_error: dict | None = None
    for _ in range(32):
        limit = 0 if offset == 0 else page_size
        listed = list_render_templates(
            renderer_contains=renderer_contains,
            template_contains=template_contains,
            limit=limit,
            offset=offset,
        )
        if not listed.get("ok"):
            last_error = listed
            break
        page = list(listed.get("templates") or [])
        if offset == 0 and not page:
            # Old host: ``if (n >= limit)`` with limit=0 yields nothing.
            listed = list_render_templates(
                renderer_contains=renderer_contains,
                template_contains=template_contains,
                limit=10000,
                offset=0,
            )
            if not listed.get("ok"):
                return listed
            page = list(listed.get("templates") or [])
            templates.extend(page)
            break
        if offset > 0 and page and templates and page[0] == templates[0]:
            break
        templates.extend(page)
        if not listed.get("truncated"):
            break
        if not page:
            break
        offset = len(templates)
    if not templates and last_error is not None:
        return last_error
    return {"ok": True, "templates": templates, "count": len(templates)}


def render_with_preset(
    output_path: str,
    preset: str = "vertical",
    *,
    start_seconds: float | None = None,
    length_seconds: float | None = None,
    edit_meta: dict[str, Any] | None = None,
    wait: bool = True,
    timeout_seconds: float = 1800,
) -> dict:
    """Named preset → async render → poll → ffprobe verify → sidecar JSON."""
    from .render.presets import RENDER_PRESETS, match_preset
    from .render.sidecar import verify_output, write_sidecar

    spec = RENDER_PRESETS.get(preset.lower())
    renderer_f = (spec or {}).get("renderer_contains") or ""
    template_f = (spec or {}).get("template_contains") or ""
    listed = _list_all_render_templates(renderer_f, template_f)
    if not listed.get("ok"):
        return listed
    matched = match_preset(preset, listed.get("templates") or [])
    if not matched.get("ok") and renderer_f:
        # Host filter missed MAGIX AVC/AAC (no "MP4" in the renderer name).
        listed = _list_all_render_templates()
        if not listed.get("ok"):
            return listed
        matched = match_preset(preset, listed.get("templates") or [])
    if not matched.get("ok"):
        return matched
    started = render_start(
        output_path=output_path,
        template_name=matched.get("template_name"),
        renderer_name=matched.get("renderer_name"),
        start_seconds=start_seconds,
        length_seconds=length_seconds,
        async_render=True,
    )
    if not started.get("ok"):
        return {**started, "preset": matched}
    job_id = started.get("job_id")
    status = started
    if wait:
        status = wait_for_render(job_id, timeout_seconds=timeout_seconds)
        if not status.get("ok"):
            return {**status, "started": started, "preset": matched}
    verify = None
    sidecar = None
    if wait and os.path.isfile(output_path):
        verify = verify_output(
            output_path,
            expect_width=spec["width"] if spec else None,
            expect_height=spec["height"] if spec else None,
            expect_audio=True,
            expect_duration=length_seconds,
        )
        sidecar = write_sidecar(output_path, edit_meta, extra={"preset": preset, "verify": verify})
    quality = None
    profile_spec = (edit_meta or {}).get("profile")
    if wait and profile_spec:
        from .quality import run_checks

        try:
            quality = run_checks(
                target=output_path,
                profile=profile_spec,
                transcript=(edit_meta or {}).get("transcript"),
                cuts=(edit_meta or {}).get("cuts") or (edit_meta or {}).get("edit"),
                edit=(edit_meta or {}).get("edit"),
                applied_features=(edit_meta or {}).get("applied_features"),
                sources=(edit_meta or {}).get("sources"),
            )
        except (OSError, ValueError) as exc:
            quality = {"ok": False, "error": str(exc)}
        if sidecar and sidecar.get("sidecar_path"):
            extra = dict((sidecar.get("extra") or {}))
            extra["quality"] = quality
            sidecar = write_sidecar(output_path, edit_meta, extra=extra)
    ok = bool(status.get("ok"))
    if quality is not None and not quality.get("ok"):
        ok = False
    return {
        "ok": ok,
        "used_mode": started.get("used_mode") or status.get("used_mode"),
        "preset": matched,
        "started": started,
        "status": status,
        "verify": verify,
        "sidecar": sidecar,
        "quality": quality,
        "output_path": output_path,
    }


def export_review_pack(
    outputs: list[str] | str,
    dest: str,
    proxy: str = "720p",
    chunk_mb: float | None = None,
    profile: Any = None,
) -> dict:
    """Write 720p/1 Mbps proxies, contact sheets, and QA into dest. Finals are only read."""
    from .render.review_pack import export_review_pack as _export

    return _export(outputs, dest, proxy=proxy, chunk_mb=chunk_mb, profile=profile)


def transport_play() -> dict:
    return _call("transport.play")


def transport_stop() -> dict:
    return _call("transport.stop")


def transport_seek(seconds: float) -> dict:
    return _call("transport.seek", {"seconds": seconds})


# ---------------- Phase 3 (2026-10-22) ----------------
def new_project(save_current_to: str | None = None, discard_unsaved: bool = False) -> dict:
    """Close the open project and start an empty one. Saves it first to save_current_to, or needs discard_unsaved=True."""
    p: dict[str, Any] = {"discard_unsaved": discard_unsaved}
    if save_current_to: p["save_current_to"] = save_current_to
    return _call("project.new", p)


def open_project(path: str, save_current_to: str | None = None, discard_unsaved: bool = False) -> dict:
    p: dict[str, Any] = {"path": path, "discard_unsaved": discard_unsaved}
    if save_current_to: p["save_current_to"] = save_current_to
    return _call("project.open", p)


def list_media() -> dict:
    """Media pool: path, offline flag, alpha mode, length."""
    return _call("media.list")


def replace_media(old_path: str, new_path: str) -> dict:
    """Swap every use of old_path for new_path (e.g. an OBS .mkv for its remuxed .mp4)."""
    return _call("media.replace", {"old_path": old_path, "new_path": new_path})


def set_media_alpha(alpha: str = "Straight", path: str | None = None, path_contains: str | None = None) -> dict:
    """Set the alpha interpretation (Undefined/None/Straight/Premultiplied...) for one file or every file whose path contains a string."""
    p: dict[str, Any] = {"alpha": alpha}
    if path: p["path"] = path
    if path_contains: p["path_contains"] = path_contains
    return _call("media.set_alpha", p)


def set_track_props(track_index: int, name: str | None = None, mute: bool | None = None, solo: bool | None = None,
                    volume: float | None = None) -> dict:
    """Rename / mute / solo a track; volume (linear, 1.0 = 0 dB) for audio tracks."""
    p: dict[str, Any] = {"track_index": track_index}
    for k, val in (("name", name), ("mute", mute), ("solo", solo), ("volume", volume)):
        if val is not None: p[k] = val
    return _call("track.set_props", p)


def clear_track_events(track_index: int) -> dict:
    return _call("track.clear_events", {"track_index": track_index})


def clear_envelope(track_index: int, envelope_type: str = "Volume") -> dict:
    """Remove a track envelope entirely (set_envelope_points only adds/updates points)."""
    return _call("envelope.clear", {"track_index": track_index, "envelope_type": envelope_type})


def set_event_gain(track_index: int, event_index: int, gain_db: float | None = None, gain: float | None = None,
                   normalize: bool | None = None) -> dict:
    """Audio event gain (dB or linear) and/or normalize."""
    p: dict[str, Any] = {"track_index": track_index, "event_index": event_index}
    if gain_db is not None: p["gain_db"] = gain_db
    if gain is not None: p["gain"] = gain
    if normalize is not None: p["normalize"] = normalize
    return _call("event.set_gain", p)


def list_render_templates(
    renderer_contains: str = "",
    template_contains: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Exact renderer/template names for render_start.

    ``limit=0`` means unlimited on the current host. ``offset`` pages past
    a previous batch. Interactive default stays 200; ``render_with_preset``
    collects the full list.
    """
    return _call(
        "render.list_templates",
        {
            "renderer_contains": renderer_contains,
            "template_contains": template_contains,
            "limit": limit,
            "offset": offset,
        },
    )


def list_editors() -> dict:
    from .backends import list_editors as _list

    return _list()


def editor_info() -> dict:
    from .backends import get_backend

    backend = get_backend()
    info = getattr(backend, "info", None)
    if callable(info):
        return info()
    return _call("editor.info")


def editor_capabilities() -> dict:
    from .backends import get_backend

    return get_backend().capabilities()


def add_marker(
    at_seconds: float,
    name: str = "marker",
    note: str = "",
    color: str = "Green",
    custom_data: str | None = None,
) -> dict:
    params: dict[str, Any] = {"at_seconds": at_seconds, "name": name, "note": note, "color": color}
    if custom_data:
        params["custom_data"] = custom_data
    return _call("marker.add", params)


def list_markers() -> dict:
    return _call("marker.list")


def delete_marker(
    at_seconds: float | None = None,
    color: str | None = None,
    custom_data: str | None = None,
    name: str | None = None,
) -> dict:
    params: dict[str, Any] = {}
    if at_seconds is not None:
        params["at_seconds"] = at_seconds
    if color is not None:
        params["color"] = color
    if custom_data is not None:
        params["custom_data"] = custom_data
    if name is not None:
        params["name"] = name
    return _call("marker.delete", params)


def resolve_create_subtitles() -> dict:
    """Studio-only: Timeline.CreateSubtitlesFromAudio()."""
    return _call("resolve.create_subtitles")


def resolve_smart_reframe(track_index: int, event_index: int) -> dict:
    """Studio-only: TimelineItem.SmartReframe()."""
    return _call("resolve.smart_reframe", {"track_index": track_index, "event_index": event_index})


def resolve_set_voice_isolation(track_index: int, event_index: int, enabled: bool = True) -> dict:
    """Studio-only: SetVoiceIsolationState on the clip / media-pool item."""
    return _call(
        "resolve.set_voice_isolation",
        {"track_index": track_index, "event_index": event_index, "enabled": enabled},
    )
