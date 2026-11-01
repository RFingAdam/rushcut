"""Track-index and timecode helpers for the Resolve backend.

VEGAS tools use a single 0-based track list and times in seconds. Resolve
uses 1-based video/audio/subtitle tracks and frames. We flatten video
tracks first, then audio, then subtitle, so `track_index=0` is V1.
"""
from __future__ import annotations

from typing import Any


def parse_fps(raw: Any, default: float = 24.0) -> float:
    if raw is None:
        return default
    text = str(raw).strip().upper().replace("DF", "").strip()
    if not text:
        return default
    try:
        value = float(text.split()[0])
    except ValueError:
        return default
    return value if value > 0 else default


def timeline_fps(timeline: Any, project: Any | None = None) -> float:
    raw = None
    if timeline is not None:
        try:
            raw = timeline.GetSetting("timelineFrameRate")
        except Exception:
            raw = None
    if not raw and project is not None:
        try:
            raw = project.GetSetting("timelineFrameRate")
        except Exception:
            raw = None
    return parse_fps(raw)


def start_frame(timeline: Any) -> int:
    if timeline is None:
        return 0
    try:
        return int(timeline.GetStartFrame() or 0)
    except Exception:
        return 0


def seconds_to_frame(seconds: float, fps: float, origin: int = 0) -> int:
    return origin + int(round(float(seconds) * float(fps)))


def frame_to_seconds(frame: int, fps: float, origin: int = 0) -> float:
    if fps <= 0:
        return 0.0
    return (int(frame) - int(origin)) / float(fps)


def frames_to_timecode(frame: int, fps: float) -> str:
    """HH:MM:SS:FF from an absolute frame number (Resolve playhead style)."""
    fps_i = max(1, int(round(float(fps))))
    total = max(0, int(frame))
    ff = total % fps_i
    secs = total // fps_i
    ss = secs % 60
    mins = secs // 60
    mm = mins % 60
    hh = mins // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def iter_folder_clips(folder: Any) -> list[Any]:
    clips: list[Any] = []
    if folder is None:
        return clips
    try:
        clips.extend(list(folder.GetClipList() or []))
    except Exception:
        pass
    try:
        for sub in folder.GetSubFolderList() or []:
            clips.extend(iter_folder_clips(sub))
    except Exception:
        pass
    return clips


class TrackRef:
    __slots__ = ("kind", "resolve_index", "flat_index", "name")

    def __init__(self, kind: str, resolve_index: int, flat_index: int, name: str):
        self.kind = kind
        self.resolve_index = resolve_index
        self.flat_index = flat_index
        self.name = name


def list_tracks(timeline: Any) -> list[TrackRef]:
    refs: list[TrackRef] = []
    if timeline is None:
        return refs
    flat = 0
    for kind in ("video", "audio", "subtitle"):
        try:
            count = int(timeline.GetTrackCount(kind) or 0)
        except Exception:
            count = 0
        for i in range(1, count + 1):
            try:
                name = timeline.GetTrackName(kind, i) or f"{kind[0].upper()}{i}"
            except Exception:
                name = f"{kind[0].upper()}{i}"
            refs.append(TrackRef(kind, i, flat, str(name)))
            flat += 1
    return refs


def track_at(timeline: Any, flat_index: int) -> TrackRef | None:
    refs = list_tracks(timeline)
    if 0 <= flat_index < len(refs):
        return refs[flat_index]
    return None


def items_on(timeline: Any, ref: TrackRef) -> list[Any]:
    try:
        items = timeline.GetItemListInTrack(ref.kind, ref.resolve_index) or []
    except Exception:
        return []
    return list(items)


def item_at(timeline: Any, flat_index: int, event_index: int) -> tuple[TrackRef | None, Any | None]:
    ref = track_at(timeline, flat_index)
    if ref is None:
        return None, None
    items = items_on(timeline, ref)
    items.sort(key=lambda it: _safe_start(it))
    if 0 <= event_index < len(items):
        return ref, items[event_index]
    return ref, None


def _safe_start(item: Any) -> int:
    try:
        return int(item.GetStart() or 0)
    except Exception:
        return 0


def clip_file_path(item: Any) -> str | None:
    try:
        mpi = item.GetMediaPoolItem()
    except Exception:
        mpi = None
    if mpi is None:
        return None
    for key in ("File Path", "Clip Path", "File Path"):
        try:
            val = mpi.GetClipProperty(key)
        except Exception:
            val = None
        if val:
            return str(val)
    try:
        meta = mpi.GetClipProperty()
        if isinstance(meta, dict):
            for key in ("File Path", "Clip Path", "File Name"):
                if meta.get(key):
                    return str(meta[key])
    except Exception:
        pass
    return None
