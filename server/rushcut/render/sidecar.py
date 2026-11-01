"""Verify a render with ffprobe and write the edit-metadata sidecar JSON."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from ..analysis.probe import probe_media

SIDECAR_KEYS = (
    "length",
    "hook_start_time",
    "caption_style",
    "zoom_count",
    "music",
    "template",
)


def sidecar_path(output_path: str) -> str:
    return output_path + ".edit.json"


def verify_output(
    path: str,
    *,
    expect_width: int | None = None,
    expect_height: int | None = None,
    expect_audio: bool = True,
    duration_tolerance: float = 0.6,
    expect_duration: float | None = None,
) -> dict[str, Any]:
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    problems: list[str] = []
    if expect_audio and not probe.get("has_audio"):
        problems.append("no audio stream")
    if expect_width and int(probe.get("width") or 0) != expect_width:
        problems.append(f"width {probe.get('width')} != {expect_width}")
    if expect_height and int(probe.get("height") or 0) != expect_height:
        problems.append(f"height {probe.get('height')} != {expect_height}")
    if expect_duration is not None:
        got = float(probe.get("duration_seconds") or 0)
        if abs(got - expect_duration) > duration_tolerance:
            problems.append(f"duration {got:.2f}s != {expect_duration:.2f}s ±{duration_tolerance}")
    return {
        "ok": not problems,
        "path": probe.get("path"),
        "probe": probe,
        "problems": problems,
    }


def write_sidecar(output_path: str, edit_meta: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = {k: (edit_meta or {}).get(k) for k in SIDECAR_KEYS}
    # Accept a few aliases from callers.
    if meta["length"] is None:
        meta["length"] = (edit_meta or {}).get("length_seconds")
    if meta["music"] is None:
        meta["music"] = bool((edit_meta or {}).get("music_on") if edit_meta and "music_on" in edit_meta else (edit_meta or {}).get("music"))
    payload = {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "edit": meta,
    }
    if extra:
        payload["extra"] = extra
    dest = sidecar_path(output_path)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    payload["sidecar_path"] = dest
    return payload
