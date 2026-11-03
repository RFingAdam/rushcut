"""Concatenate a recording session and map session time back to source files.

Groups files whose recording-start gap is within a threshold, remuxes
unfinalized containers (never in place), and returns an offset map so other
tools can accept session time. No NLE required.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media

DEFAULT_GAP_S = 600.0
_TS_RE = re.compile(
    r"(?P<y>\d{4})[-_](?P<m>\d{2})[-_](?P<d>\d{2})[ T_-]"
    r"(?P<H>\d{2})[-_.](?P<M>\d{2})[-_.](?P<S>\d{2})"
)


def _ok_path(path: str, label: str = "path") -> dict[str, Any] | None:
    if not path:
        return {"ok": False, "error": f"{label} is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    return None


def recording_start_seconds(path: str) -> tuple[float, str]:
    """Best-effort recording start as unix seconds. Filename timestamp, else mtime."""
    name = os.path.basename(path)
    match = _TS_RE.search(name)
    if match:
        try:
            dt = datetime(
                int(match.group("y")),
                int(match.group("m")),
                int(match.group("d")),
                int(match.group("H")),
                int(match.group("M")),
                int(match.group("S")),
            )
            return dt.replace(tzinfo=UTC).timestamp(), "filename"
        except ValueError:
            pass
    return os.path.getmtime(path), "mtime"


def is_unfinalized(probe: dict[str, Any] | None) -> bool:
    if not probe or not probe.get("ok"):
        return True
    if probe.get("unfinalized_container") or probe.get("header_duration_missing"):
        return True
    return float(probe.get("duration_seconds") or 0) <= 0


def repair_media(path: str, output_path: str | None = None) -> dict[str, Any]:
    """Stream-copy remux into a finalized file. Never modifies the source."""
    err = _ok_path(path)
    if err:
        return err
    src = os.path.abspath(path)
    if not output_path:
        root, ext = os.path.splitext(src)
        output_path = f"{root}.repaired{ext or '.mkv'}"
    output_path = os.path.abspath(output_path)
    if os.path.normcase(output_path) == os.path.normcase(src):
        return {"ok": False, "error": "repair_media refuses to overwrite the source"}
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-fflags",
                "+genpts",
                "-i",
                src,
                "-c",
                "copy",
                output_path,
            ],
            timeout=180,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc), "path": src}
    probe = probe_media(output_path)
    return {
        "ok": bool(probe.get("ok")),
        "path": src,
        "output_path": output_path,
        "duration_seconds": probe.get("duration_seconds"),
        "unfinalized_container": bool(probe.get("unfinalized_container")),
        "probe": probe,
    }


def _concat_copy(paths: list[str], output_path: str) -> dict[str, Any]:
    list_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for p in paths:
            escaped = p.replace("\\", "/").replace("'", r"'\''")
            list_file.write(f"file '{escaped}'\n")
        list_file.close()
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file.name,
                "-c",
                "copy",
                output_path,
            ],
            timeout=300,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            os.remove(list_file.name)
        except OSError:
            pass
    return {"ok": True, "output_path": os.path.abspath(output_path), "count": len(paths)}


def lookup_session_time(offset_map: list[dict[str, Any]] | None, session_seconds: float) -> dict[str, Any]:
    """Map a session timestamp onto a source file + file-local time."""
    if not offset_map:
        return {"ok": False, "error": "offset_map is required"}
    try:
        t = float(session_seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "session_seconds must be a number"}
    if t < 0:
        return {"ok": False, "error": "session_seconds must be >= 0"}
    last = None
    for index, entry in enumerate(offset_map):
        try:
            offset = float(entry.get("offset_seconds") or 0)
            duration = float(entry.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            continue
        last = (index, entry, offset, duration)
        end = offset + duration
        if duration <= 0:
            continue
        if offset <= t < end or (index == len(offset_map) - 1 and abs(t - end) <= 0.05):
            file_seconds = max(0.0, min(duration, t - offset))
            return {
                "ok": True,
                "path": entry.get("path"),
                "index": index,
                "offset_seconds": offset,
                "duration_seconds": duration,
                "session_seconds": round(t, 6),
                "file_seconds": round(file_seconds, 6),
            }
    if last is not None:
        index, entry, offset, duration = last
        return {
            "ok": False,
            "error": f"session time {t} is outside 0..{offset + duration}",
            "path": entry.get("path"),
            "index": index,
            "offset_seconds": offset,
            "duration_seconds": duration,
            "session_seconds": round(t, 6),
        }
    return {"ok": False, "error": "offset_map is empty"}


def _group_files(
    files: list[dict[str, Any]],
    gap_threshold_s: float,
) -> list[list[dict[str, Any]]]:
    ordered = sorted(files, key=lambda f: (f["recording_start"], f["order"]))
    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev_end: float | None = None
    for item in ordered:
        start = float(item["recording_start"])
        if current and prev_end is not None and start - prev_end > gap_threshold_s:
            sessions.append(current)
            current = []
        current.append(item)
        prev_end = start + float(item["duration_seconds"] or 0)
    if current:
        sessions.append(current)
    return sessions


def _offset_map(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offset = 0.0
    rows: list[dict[str, Any]] = []
    for item in files:
        duration = float(item["duration_seconds"] or 0)
        rows.append(
            {
                "path": item["source_path"],
                "work_path": item["work_path"],
                "offset_seconds": round(offset, 6),
                "duration_seconds": round(duration, 6),
                "start_session_s": round(offset, 6),
                "end_session_s": round(offset + duration, 6),
                "recording_start": item["recording_start"],
                "recording_start_source": item["recording_start_source"],
                "remuxed": bool(item.get("remuxed")),
            }
        )
        offset += duration
    return rows


def stitch_session(
    paths: list[str] | None,
    *,
    gap_threshold_s: float = DEFAULT_GAP_S,
    output_path: str | None = None,
    session_index: int | None = None,
    remux_unfinalized: bool = True,
) -> dict[str, Any]:
    """Group recording files into sessions, remux broken containers, concat, return offsets."""
    if not paths:
        return {"ok": False, "error": "paths is required"}
    try:
        gap = float(gap_threshold_s)
    except (TypeError, ValueError):
        return {"ok": False, "error": "gap_threshold_s must be a number"}
    if gap < 0:
        return {"ok": False, "error": "gap_threshold_s must be >= 0"}

    prepared: list[dict[str, Any]] = []
    remuxed: list[str] = []
    warnings: list[str] = []
    tmp_dir: str | None = None
    for order, raw in enumerate(paths):
        err = _ok_path(str(raw))
        if err:
            return err
        src = os.path.abspath(str(raw))
        probe = probe_media(src)
        work = src
        did_remux = False
        if remux_unfinalized and is_unfinalized(probe):
            if tmp_dir is None:
                tmp_dir = tempfile.mkdtemp(prefix="rushcut-session-")
            repaired_path = os.path.join(tmp_dir, f"{order}{os.path.splitext(src)[1] or '.mkv'}")
            repaired = repair_media(src, repaired_path)
            if not repaired.get("ok"):
                return {**repaired, "error": f"remux failed for {src}: {repaired.get('error')}"}
            work = repaired["output_path"]
            probe = repaired.get("probe") or probe_media(work)
            did_remux = True
            remuxed.append(src)
            warnings.append("unfinalized_container")
        start, start_source = recording_start_seconds(src)
        prepared.append(
            {
                "order": order,
                "source_path": src,
                "work_path": work,
                "probe": probe,
                "duration_seconds": float(probe.get("duration_seconds") or 0),
                "recording_start": start,
                "recording_start_source": start_source,
                "remuxed": did_remux,
            }
        )

    grouped = _group_files(prepared, gap)
    sessions: list[dict[str, Any]] = []
    for index, group in enumerate(grouped):
        offset_map = _offset_map(group)
        duration = offset_map[-1]["end_session_s"] if offset_map else 0.0
        sessions.append(
            {
                "index": index,
                "count": len(group),
                "duration_seconds": duration,
                "files": [item["source_path"] for item in group],
                "offset_map": offset_map,
            }
        )

    if not sessions:
        return {"ok": False, "error": "no sessions"}

    chosen = 0
    if session_index is not None:
        if session_index < 0 or session_index >= len(sessions):
            return {"ok": False, "error": f"session_index {session_index} is out of range"}
        chosen = session_index
    elif len(sessions) > 1:
        chosen = max(range(len(sessions)), key=lambda i: sessions[i]["duration_seconds"])

    target = sessions[chosen]
    result: dict[str, Any] = {
        "ok": True,
        "session_count": len(sessions),
        "session_index": chosen,
        "duration_seconds": target["duration_seconds"],
        "offset_map": target["offset_map"],
        "files": target["files"],
        "sessions": sessions,
        "gap_threshold_s": gap,
        "remuxed": remuxed,
        "warnings": sorted(set(warnings)),
    }

    if output_path:
        output_path = os.path.abspath(output_path)
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        work_paths = [row["work_path"] for row in target["offset_map"]]
        method = "copy"
        copied = _concat_copy(work_paths, output_path)
        if not copied.get("ok"):
            from .edit_ops import concat_media

            method = "reencode"
            copied = concat_media(work_paths, output_path)
            if not copied.get("ok"):
                return copied
        stitched = probe_media(output_path)
        result["output_path"] = output_path
        result["output_duration_seconds"] = stitched.get("duration_seconds")
        result["concat"] = method
        sidecar = output_path + ".session.json"
        try:
            import json

            with open(sidecar, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "ok": True,
                        "output_path": output_path,
                        "duration_seconds": result["duration_seconds"],
                        "offset_map": [
                            {k: v for k, v in row.items() if k != "work_path"} for row in target["offset_map"]
                        ],
                    },
                    fh,
                    indent=2,
                )
                fh.write("\n")
            result["sidecar_path"] = sidecar
        except OSError:
            pass
        # Prefer the measured container duration when it is close to the sum.
        measured = float(stitched.get("duration_seconds") or 0)
        if measured > 0:
            result["duration_seconds"] = measured
    return result
