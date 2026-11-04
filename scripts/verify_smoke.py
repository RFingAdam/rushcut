#!/usr/bin/env python3
"""Tiny fixture render through the real ``rushcut apply`` CLI.

Asserts exit code, output files, and duration. Failures name the check so
``make verify`` can fail fast with a fix.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


EXPECTED_SECONDS = 0.8
DURATION_TOLERANCE = 0.15
SMOKE_TIMEOUT = 120


def _fail(check: str, detail: str) -> None:
    print(f"VERIFY FAILED: smoke — {check}: {detail}", file=sys.stderr)
    raise SystemExit(1)


def _rushcut_cmd() -> list[str]:
    sibling = Path(sys.executable).resolve().parent / "rushcut"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return [str(sibling)]
    return [sys.executable, "-m", "rushcut.cli"]


def _ffmpeg() -> str:
    return os.environ.get("FFMPEG_BIN") or "ffmpeg"


def _write_clip(dest: Path) -> None:
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=160x90:r=24:d=1.0",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1.0",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        _fail("fixture", f"ffmpeg failed to write {dest}: {proc.stderr[-400:]}")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=SMOKE_TIMEOUT,
    )


def _require_duration(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)):
        _fail("duration", f"{label} missing duration_seconds: {value!r}")
    duration = float(value)
    if abs(duration - EXPECTED_SECONDS) > DURATION_TOLERANCE:
        _fail(
            "duration",
            f"{label} reported {duration:.3f}s, expected "
            f"{EXPECTED_SECONDS:.3f}s ± {DURATION_TOLERANCE:.3f}s",
        )
    return duration


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rushcut-verify-") as raw:
        work = Path(raw)
        clip = work / "src.mp4"
        _write_clip(clip)
        edit_path = work / "fixture.edit.json"
        edit_path.write_text(
            json.dumps(
                {
                    "schema": "rushcut.edit/v1",
                    "output": {"width": 160, "height": 90, "fps": 24},
                    "join_fade_ms": 0,
                    "segments": [
                        {"source": str(clip), "in": 0.0, "out": EXPECTED_SECONDS}
                    ],
                }
            ),
            encoding="utf-8",
        )
        out = work / "out.mp4"
        rushcut = _rushcut_cmd()
        applied = _run([*rushcut, "apply", str(edit_path), "--output", str(out)])
        if applied.returncode != 0:
            _fail(
                "exit code",
                f"rushcut apply exited {applied.returncode}, expected 0\n"
                f"stdout:\n{applied.stdout[-800:]}\nstderr:\n{applied.stderr[-800:]}",
            )
        if not out.is_file() or out.stat().st_size <= 0:
            _fail("output files", f"missing or empty render: {out}")
        sidecar = Path(str(out) + ".edit.json")
        if not sidecar.is_file():
            _fail("output files", f"missing sidecar: {sidecar}")
        try:
            payload = json.loads(applied.stdout)
        except json.JSONDecodeError as exc:
            _fail(
                "exit code",
                f"rushcut apply stdout was not JSON: {exc}\n{applied.stdout[-400:]}",
            )
        if not isinstance(payload, dict):
            _fail("exit code", f"rushcut apply JSON root must be an object: {payload!r}")
        _require_duration(payload.get("duration_seconds"), "rushcut apply")
        checked = _run([*rushcut, "check-media", str(out)])
        if checked.returncode != 0:
            _fail(
                "exit code",
                f"rushcut check-media exited {checked.returncode}, expected 0\n"
                f"{checked.stdout[-400:]}\n{checked.stderr[-400:]}",
            )
        try:
            media = json.loads(checked.stdout)
        except json.JSONDecodeError as exc:
            _fail(
                "duration",
                f"rushcut check-media stdout was not JSON: {exc}\n{checked.stdout[-400:]}",
            )
        if not isinstance(media, dict):
            _fail("duration", f"check-media JSON root must be an object: {media!r}")
        probed = _require_duration(media.get("duration_seconds"), "rushcut check-media")
        print(
            f"smoke passed: {out.name} duration={probed:.3f}s "
            f"exit=0 sidecar={sidecar.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
