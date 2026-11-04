"""Higher-level edit helpers built on host primitives and FFmpeg."""
from .helpers import apply_crossfade, apply_ducking
from .schema import looks_like_edit
from .timeline import apply_timeline as apply_file_effect

__all__ = [
    "apply_crossfade",
    "apply_ducking",
    "apply_file_effect",
    "apply_timeline",
]


def apply_timeline(
    effect: str | dict | None = None,
    path: str | None = None,
    output_path: str | None = None,
    track_index: int | None = None,
    replace_media: bool = True,
    edit_json: str | dict | None = None,
    output: str | None = None,
    backend: str | None = None,
    **params,
):
    """Audio-effect dispatcher *or* declarative edit.json renderer.

    ``apply_timeline("bleep", path=..., spans=...)`` keeps the file-level
    audio ops. ``apply_timeline(edit_json, output)`` renders a multi-segment
    timeline on FFmpeg (or builds it on VEGAS / Resolve).
    """
    payload = edit_json
    dest = output or output_path
    if payload is None and looks_like_edit(effect):
        payload = effect
        effect = None
    if payload is not None:
        from .apply import apply_edit

        return apply_edit(payload, dest, backend=backend)
    if not effect:
        return {"ok": False, "error": "edit_json or effect is required", "field": "edit_json"}
    return apply_file_effect(
        str(effect),
        path=path,
        output_path=dest,
        track_index=track_index,
        replace_media=replace_media,
        **params,
    )
