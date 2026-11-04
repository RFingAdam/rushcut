"""File-level ffmpeg primitives. No NLE required.

These are the headless building blocks an editor-agnostic agent can chain:
cut/trim, concat, reframe, audio mix, and loudness normalize. Timeline
composition (tracks, fades, overlays) lives on the FFmpeg backend.
"""
from __future__ import annotations

import os
from typing import Any

from ..output_policy import check_output_path, make_work_dir, work_named_tempfile
from .ffmpeg_util import encode_timeout_seconds, which_ffmpeg
from .probe import attach_container_warnings, probe_media, streams_compatible


def _ok_path(path: str, label: str = "path") -> dict[str, Any] | None:
    if not path:
        return {"ok": False, "error": f"{label} is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    return None


def _ffmpeg_out(
    cmd: list[str],
    output_path: str,
    *,
    timeout: float = 180,
    verify_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ..render.atomic import run_ffmpeg_atomic

    refused = check_output_path(output_path)
    if refused:
        return refused
    return run_ffmpeg_atomic(cmd, output_path, timeout=timeout, verify_kwargs=verify_kwargs)


def cut_media(
    path: str | None = None,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    output_path: str | None = None,
    length_seconds: float | None = None,
    offset_map: list[dict[str, Any]] | None = None,
    session_start: float | None = None,
    session_end: float | None = None,
) -> dict[str, Any]:
    """Trim a media file to [start, end) and write a new file.

    Pass ``offset_map`` with ``session_start`` / ``session_end`` to cut in
    session time (from ``stitch_session``). A range that stays in one file
    is a single trim; a range that spans files concatenates the pieces.
    """
    if offset_map is not None and session_start is not None:
        from .session import lookup_session_time

        start_hit = lookup_session_time(offset_map, session_start)
        if not start_hit.get("ok"):
            return start_hit
        if session_end is None:
            if length_seconds is not None:
                session_end = float(session_start) + float(length_seconds)
            else:
                return {"ok": False, "error": "session_end or length_seconds is required"}
        end_hit = lookup_session_time(offset_map, float(session_end))
        if not end_hit.get("ok"):
            return end_hit
        if start_hit["path"] == end_hit["path"]:
            path = str(start_hit["path"])
            start_seconds = float(start_hit["file_seconds"])
            end_seconds = float(end_hit["file_seconds"])
            length_seconds = None
        else:
            pieces: list[str] = []
            tmp_dir = make_work_dir("rushcut-session-cut-")
            start_index = int(start_hit["index"])
            end_index = int(end_hit["index"])
            for index in range(start_index, end_index + 1):
                entry = offset_map[index]
                file_path = str(entry.get("path") or entry.get("work_path"))
                file_start = float(start_hit["file_seconds"]) if index == start_index else 0.0
                if index == end_index:
                    file_end = float(end_hit["file_seconds"])
                else:
                    file_end = float(entry.get("duration_seconds") or 0)
                piece = os.path.join(tmp_dir, f"piece-{index}.mp4")
                cut = cut_media(file_path, file_start, file_end, piece)
                if not cut.get("ok"):
                    return cut
                pieces.append(piece)
            if not output_path:
                output_path = os.path.join(tmp_dir, "session-cut.mp4")
            cat = concat_media(pieces, output_path)
            if cat.get("ok"):
                cat["session_start"] = float(session_start)
                cat["session_end"] = float(session_end)
            return cat
    if not path:
        return {"ok": False, "error": "path is required"}
    err = _ok_path(path)
    if err:
        return err
    start = max(0.0, float(start_seconds))
    if length_seconds is not None:
        length = float(length_seconds)
        end = start + length
    elif end_seconds is not None:
        end = float(end_seconds)
        length = end - start
    else:
        return {"ok": False, "error": "end_seconds or length_seconds is required"}
    if length <= 0:
        return {"ok": False, "error": "cut length must be positive"}
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.cut{ext or '.mp4'}"
    written = _ffmpeg_out(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-ss",
            f"{start:.6f}",
            "-i",
            path,
            "-t",
            f"{length:.6f}",
            "-avoid_negative_ts",
            "make_zero",
        ],
        output_path,
    )
    if not written.get("ok"):
        return attach_container_warnings(written, probe_media(path, speech_ratio=False))
    return attach_container_warnings(
        {
            "ok": True,
            "output_path": os.path.abspath(output_path),
            "start_seconds": start,
            "end_seconds": start + length,
            "length_seconds": length,
            "qa_path": written.get("qa_path"),
            "verify": written.get("verify"),
        },
        probe_media(path, speech_ratio=False),
    )


def concat_media(
    paths: list[str],
    output_path: str,
    timeout: float | None = None,
    preset: str = "medium",
    crf: int = 23,
) -> dict[str, Any]:
    """Concatenate media files in order.

    Stream-copies when every input shares codec parameters. Otherwise
    re-encodes with a quality ``preset`` / ``crf`` (default medium/23,
    not ultrafast). Timeout scales with program length when omitted.
    """
    if not paths:
        return {"ok": False, "error": "paths is required"}
    files: list[str] = []
    for p in paths:
        err = _ok_path(p)
        if err:
            return err
        files.append(os.path.abspath(p))
    if not output_path:
        return {"ok": False, "error": "output_path is required"}
    probes = [probe_media(p) for p in files]
    copy = streams_compatible(probes)
    used_preset = str(preset or "medium")
    used_crf = int(crf)
    total_duration = sum(float(p.get("duration_seconds") or 0) for p in probes)
    first = probes[0] if probes else {}
    fps = float(first.get("fps") or 0) or None
    has_audio = any(p.get("has_audio") for p in probes)
    has_video = any(p.get("has_video") for p in probes)
    used_timeout = encode_timeout_seconds(
        total_duration,
        width=first.get("width"),
        height=first.get("height"),
        fps=fps,
        preset=used_preset,
        copy=copy,
        override=timeout,
    )
    verify_kwargs = {"expect_audio": has_audio}

    def _concat_cmd(list_path: str, *, stream_copy: bool) -> list[str]:
        cmd = [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
        ]
        if stream_copy:
            # Rebuild timestamps so concat-demuxer joins pass a full decode.
            cmd.extend(["-fflags", "+genpts"])
        cmd.extend(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
            ]
        )
        if stream_copy:
            cmd.extend(["-c", "copy"])
            return cmd
        if has_video:
            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    used_preset,
                    "-crf",
                    str(used_crf),
                    "-pix_fmt",
                    "yuv420p",
                ]
            )
        if has_audio:
            cmd.extend(["-c:a", "aac"])
        elif has_video:
            cmd.extend(["-an"])
        return cmd

    list_file = work_named_tempfile(".txt")
    try:
        for p in files:
            escaped = p.replace("\\", "/").replace("'", r"'\''")
            list_file.write(f"file '{escaped}'\n")
        list_file.close()
        written = _ffmpeg_out(
            _concat_cmd(list_file.name, stream_copy=copy),
            output_path,
            timeout=used_timeout,
            verify_kwargs=verify_kwargs,
        )
        if copy and not written.get("ok"):
            copy = False
            used_timeout = encode_timeout_seconds(
                total_duration,
                width=first.get("width"),
                height=first.get("height"),
                fps=fps,
                preset=used_preset,
                copy=False,
                override=timeout,
            )
            written = _ffmpeg_out(
                _concat_cmd(list_file.name, stream_copy=False),
                output_path,
                timeout=used_timeout,
                verify_kwargs=verify_kwargs,
            )
            if written.get("ok"):
                written["stream_copy_fallback"] = "reencode"
    finally:
        try:
            os.remove(list_file.name)
        except OSError:
            pass
    if not written.get("ok"):
        return written
    written["count"] = len(files)
    written["stream_copy"] = copy
    written["preset"] = None if copy else used_preset
    written["crf"] = None if copy else used_crf
    written["timeout_seconds"] = used_timeout
    return written


def reframe_media(
    path: str,
    width: int,
    height: int,
    output_path: str | None = None,
    pan_x: float = 0.0,
    pan_y: float = 0.0,
    scale: float = 1.0,
) -> dict[str, Any]:
    """Cover-crop (and optional punch-in) to a target frame size."""
    err = _ok_path(path)
    if err:
        return err
    width = int(width)
    height = int(height)
    if width < 2 or height < 2:
        return {"ok": False, "error": "width and height must be >= 2"}
    zoom = max(1.0, float(scale))
    px = max(-1.0, min(1.0, float(pan_x)))
    py = max(-1.0, min(1.0, float(pan_y)))
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.{width}x{height}{ext or '.mp4'}"
    # Cover the zoomed canvas, then crop WxH. pan -1..1 shifts the crop window.
    vf = (
        f"scale={width * zoom}:{height * zoom}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-{width})/2*(1+{px}):(ih-{height})/2*(1+{py})"
    )
    probe = probe_media(path)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        path,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
    ]
    if probe.get("has_audio"):
        cmd.extend(["-c:a", "aac"])
    else:
        cmd.extend(["-an"])
    written = _ffmpeg_out(cmd, output_path)
    if not written.get("ok"):
        return written
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "width": width,
        "height": height,
        "scale": zoom,
        "pan_x": px,
        "pan_y": py,
        "qa_path": written.get("qa_path"),
        "verify": written.get("verify"),
    }


def mix_audio(paths: list[str], output_path: str, normalize_mix: bool = False) -> dict[str, Any]:
    """Mix audio streams from one or more files into a single audio (or A/V) file."""
    if not paths:
        return {"ok": False, "error": "paths is required"}
    files: list[str] = []
    for p in paths:
        err = _ok_path(p)
        if err:
            return err
        files.append(os.path.abspath(p))
    if not output_path:
        return {"ok": False, "error": "output_path is required"}
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    for p in files:
        cmd.extend(["-i", p])
    n = len(files)
    labels = "".join(f"[{i}:a]" for i in range(n))
    mix = f"{labels}amix=inputs={n}:duration=longest:normalize={'1' if normalize_mix else '0'}[a]"
    cmd.extend(["-filter_complex", mix, "-map", "[a]", "-c:a", "aac"])
    written = _ffmpeg_out(cmd, output_path)
    if not written.get("ok"):
        return written
    written["count"] = n
    return written


def normalize_audio(
    path: str,
    output_path: str | None = None,
    integrated_lufs: float = -16.0,
) -> dict[str, Any]:
    """Loudness-normalize a file with ffmpeg loudnorm."""
    err = _ok_path(path)
    if err:
        return err
    if not output_path:
        root, ext = os.path.splitext(path)
        output_path = f"{root}.norm{ext or '.mp4'}"
    probe = probe_media(path)
    if not probe.get("has_audio"):
        return {"ok": False, "error": "no audio stream to normalize", "path": path}
    af = f"loudnorm=I={float(integrated_lufs)}:TP=-1.5:LRA=11"
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", path, "-af", af]
    if probe.get("has_video"):
        cmd.extend(["-c:v", "copy"])
    cmd.extend(["-c:a", "aac"])
    written = _ffmpeg_out(cmd, output_path)
    if not written.get("ok"):
        return written
    written["integrated_lufs"] = float(integrated_lufs)
    return written
