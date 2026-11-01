"""detect_scenes — ffmpeg scene-change timestamps. No VEGAS required."""
from __future__ import annotations

import os
import re
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media

_PTS = re.compile(r"pts_time:([0-9.]+)")
_SCENE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def detect_scenes(path: str, threshold: float = 0.30) -> dict[str, Any]:
    """Return scene-cut timestamps using ffmpeg's scene score.

    `threshold` is the lavfi scene score (0..1). 0.3 is a reasonable default
    for hard cuts; lower it for subtler edits.
    """
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return {"ok": False, "error": "threshold must be a number"}
    threshold = min(0.99, max(0.01, threshold))

    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    if not probe.get("has_video"):
        return {"ok": False, "error": "no video stream", "path": path}

    # showinfo prints pts_time on selected frames (scene cuts).
    filt = f"select='gt(scene,{threshold})',showinfo"
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-filter:v",
        filt,
        "-f",
        "null",
        "-",
    ]
    try:
        proc = run(cmd, timeout=max(60.0, float(probe.get("duration_seconds") or 0) * 2 + 30), check=False)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}

    log = (proc.stderr or "") + "\n" + (proc.stdout or "")
    cuts: list[dict[str, Any]] = []
    pending_t: float | None = None
    for line in log.splitlines():
        m_pts = _PTS.search(line)
        if m_pts:
            pending_t = float(m_pts.group(1))
        m_sc = _SCENE.search(line)
        if pending_t is not None:
            score = float(m_sc.group(1)) if m_sc else None
            # Dedup near-identical timestamps (showinfo can repeat).
            if not cuts or abs(pending_t - cuts[-1]["at_seconds"]) > 0.04:
                item: dict[str, Any] = {"at_seconds": round(pending_t, 4)}
                if score is not None:
                    item["score"] = round(score, 4)
                cuts.append(item)
            pending_t = None

    duration = float(probe.get("duration_seconds") or 0)
    bounds = [0.0] + [c["at_seconds"] for c in cuts if 0.04 < c["at_seconds"] < duration - 0.04]
    if duration > 0 and (not bounds or bounds[-1] < duration - 0.04):
        bounds.append(duration)
    scenes = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if end - start < 0.04:
            continue
        scenes.append(
            {
                "index": len(scenes),
                "start_seconds": round(start, 4),
                "end_seconds": round(end, 4),
                "length_seconds": round(end - start, 4),
            }
        )
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "threshold": threshold,
        "duration_seconds": duration,
        "cut_count": len(cuts),
        "cuts": cuts,
        "scenes": scenes,
    }
