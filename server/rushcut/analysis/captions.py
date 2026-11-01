"""SRT / ASS captions plus optional ffmpeg burn-in."""
from __future__ import annotations

import os
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg


def _ts_srt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _ts_ass(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, c = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{c:02d}"


def _group_words(
    words: list[dict[str, Any]],
    max_words: int = 4,
    max_seconds: float = 1.6,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        if not cur:
            cur = [w]
            continue
        span = float(w.get("end_seconds") or 0) - float(cur[0].get("start_seconds") or 0)
        if len(cur) >= max_words or span >= max_seconds:
            groups.append(_flush(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        groups.append(_flush(cur))
    return groups


def _flush(cur: list[dict[str, Any]]) -> dict[str, Any]:
    text = " ".join((w.get("word") or "").strip() for w in cur).strip()
    return {
        "start_seconds": float(cur[0].get("start_seconds") or 0),
        "end_seconds": float(cur[-1].get("end_seconds") or cur[0].get("start_seconds") or 0),
        "text": text,
    }


def _cues_from_input(
    segments: list[dict[str, Any]] | None,
    words: list[dict[str, Any]] | None,
    group_words: bool,
) -> list[dict[str, Any]]:
    if words and group_words:
        return _group_words(words)
    cues: list[dict[str, Any]] = []
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start_seconds") or seg.get("start") or 0)
        end = float(seg.get("end_seconds") or seg.get("end") or start + 1.5)
        if end <= start:
            end = start + 1.2
        cues.append({"start_seconds": start, "end_seconds": end, "text": text})
    return cues


def _ass_header(style: str) -> str:
    key = (style or "default").lower()
    if key in {"shorts", "vertical", "portrait"}:
        # Generic 9:16 layout. "shorts" is a deprecated alias of "vertical".
        play_x, play_y = 1080, 1920
        font, size, outline, margin_v = "Arial", 64, 6, 160
    else:
        play_x, play_y = 1920, 1080
        font, size, outline, margin_v = "Arial", 48, 4, 40
    primary, outline_c = "&H00FFFFFF", "&H00000000"
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_x}\n"
        f"PlayResY: {play_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},&H000000FF,{outline_c},"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{outline},2,2,60,60,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def generate_captions(
    path: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    words: list[dict[str, Any]] | None = None,
    output_dir: str | None = None,
    style: str = "default",
    group_words: bool = True,
    stem: str | None = None,
) -> dict[str, Any]:
    """Write SRT + ASS next to the media (or output_dir). No NLE required."""
    cues = _cues_from_input(segments, words, group_words)
    if not cues:
        return {"ok": False, "error": "no caption cues (pass segments or words)"}

    base = stem
    if not base:
        if path:
            base = os.path.splitext(os.path.basename(path))[0]
        else:
            base = "captions"
    dest = output_dir or (os.path.dirname(os.path.abspath(path)) if path else os.getcwd())
    os.makedirs(dest, exist_ok=True)
    srt_path = os.path.join(dest, base + ".srt")
    ass_path = os.path.join(dest, base + ".ass")

    srt_lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        srt_lines.append(str(i))
        srt_lines.append(f"{_ts_srt(cue['start_seconds'])} --> {_ts_srt(cue['end_seconds'])}")
        srt_lines.append(cue["text"])
        srt_lines.append("")
    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(srt_lines))

    ass = [_ass_header(style)]
    for cue in cues:
        text = cue["text"].replace("\n", "\\N")
        ass.append(
            f"Dialogue: 0,{_ts_ass(cue['start_seconds'])},{_ts_ass(cue['end_seconds'])},"
            f"Default,,0,0,0,,{text}\n"
        )
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.writelines(ass)

    return {
        "ok": True,
        "style": style,
        "cue_count": len(cues),
        "srt_path": srt_path,
        "ass_path": ass_path,
        "cues": cues,
    }


def burn_captions(
    media_path: str,
    ass_path: str,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Burn an ASS file into a new MP4 with ffmpeg. No VEGAS required."""
    if not os.path.isfile(media_path):
        return {"ok": False, "error": f"file not found: {media_path}"}
    if not os.path.isfile(ass_path):
        return {"ok": False, "error": f"ASS not found: {ass_path}"}
    if not output_path:
        root, ext = os.path.splitext(media_path)
        output_path = root + ".captioned" + (ext or ".mp4")
    # Escape the ASS path for the subtitles filter (Windows + Linux).
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
    filt = f"ass='{ass_esc}'"
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                media_path,
                "-vf",
                filt,
                "-c:a",
                "copy",
                output_path,
            ],
            timeout=300,
        )
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "output_path": os.path.abspath(output_path), "ass_path": ass_path}
