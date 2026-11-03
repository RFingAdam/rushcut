"""Atomic render outputs: write ``.partial``, verify, then rename.

A failed or interrupted encode must never leave a truncated file under
the deliverable name. The sidecar ``<name>.qa.json`` records duration,
streams, decode-error count and MD5 after a passing check.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import threading
from datetime import UTC, datetime
from typing import Any

from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media
from ..output_policy import check_output_path, make_work_dir

log = logging.getLogger("rushcut")

_PARTIAL_MARK = ".partial"
_INTERRUPT_STATES = frozenset({"interrupted", "interrupt", "aborted"})


def partial_path(final_path: str) -> str:
    """``out.mp4`` → ``out.partial.mp4``; already-partial paths are unchanged."""
    if not final_path:
        return final_path
    if _is_partial(final_path):
        return final_path
    root, ext = os.path.splitext(final_path)
    return f"{root}{_PARTIAL_MARK}{ext or '.mp4'}"


def final_path_from_partial(path: str) -> str:
    if not _is_partial(path):
        return path
    root, ext = os.path.splitext(path)
    if root.endswith(_PARTIAL_MARK):
        return root[: -len(_PARTIAL_MARK)] + ext
    return path.replace(_PARTIAL_MARK, "", 1)


def qa_path(final_path: str) -> str:
    root, _ext = os.path.splitext(final_path_from_partial(final_path))
    return root + ".qa.json"


def _is_partial(path: str) -> bool:
    name = os.path.basename(path)
    root, _ext = os.path.splitext(name)
    return root.endswith(_PARTIAL_MARK) or _PARTIAL_MARK + "." in name


def file_md5(path: str) -> str | None:
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def has_moov_atom(path: str) -> bool:
    """True when the container has a ``moov`` box (or is not MP4/MOV)."""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in {".mp4", ".m4a", ".m4v", ".mov", ".partial", ""} and not path.endswith(".mp4"):
        # Non-ISO-BMFF: treat a readable probe as enough.
        probe = probe_media(path)
        return bool(probe.get("ok"))
    try:
        with open(path, "rb") as fh:
            sample = fh.read(1 << 20)
            if b"moov" in sample:
                return True
            size = os.path.getsize(path)
            if size > 1 << 20:
                fh.seek(max(0, size - (1 << 20)))
                if b"moov" in fh.read(1 << 20):
                    return True
    except OSError:
        return False
    return False


_ERROR_LINE = re.compile(r"(?i)\b(error|invalid|corrupt|failed)\b")


def count_decode_errors(path: str, *, timeout: float = 180) -> int:
    """Full decode to ``-f null -``; count error-level messages."""
    if not path or not os.path.isfile(path):
        return 1
    try:
        proc = run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-v",
                "error",
                "-xerror",
                "-i",
                path,
                "-f",
                "null",
                "-",
            ],
            timeout=timeout,
            check=False,
        )
    except FfmpegError:
        return 1
    err = proc.stderr or ""
    if isinstance(err, (bytes, bytearray)):
        err = err.decode("utf-8", "replace")
    lines = [ln for ln in str(err).splitlines() if ln.strip() and _ERROR_LINE.search(ln)]
    if proc.returncode not in (0, None) and not lines:
        return 1
    return len(lines)


def write_qa_sidecar(final_path: str, verify: dict[str, Any]) -> str:
    dest = qa_path(final_path)
    parent = os.path.dirname(os.path.abspath(dest))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "ok": bool(verify.get("ok")),
        "path": os.path.abspath(final_path),
        "written_at": datetime.now(UTC).isoformat(),
        "duration_seconds": (verify.get("probe") or {}).get("duration_seconds"),
        "streams": {
            "has_video": (verify.get("probe") or {}).get("has_video"),
            "has_audio": (verify.get("probe") or {}).get("has_audio"),
            "width": (verify.get("probe") or {}).get("width"),
            "height": (verify.get("probe") or {}).get("height"),
            "video_codec": (verify.get("probe") or {}).get("video_codec"),
            "audio_codec": (verify.get("probe") or {}).get("audio_codec"),
        },
        "decode_errors": verify.get("decode_errors"),
        "moov_ok": verify.get("moov_ok"),
        "md5": verify.get("md5"),
    }
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return dest


def promote_partial(
    partial: str,
    final_path: str,
    *,
    verify: dict[str, Any] | None = None,
    expect_width: int | None = None,
    expect_height: int | None = None,
    expect_audio: bool = True,
    expect_duration: float | None = None,
) -> dict[str, Any]:
    """Verify *partial* and rename to *final_path* only when the check passes.

    An existing file at *final_path* is left untouched on failure.
    """
    from .sidecar import verify_output

    if not partial or not os.path.isfile(partial):
        return {
            "ok": False,
            "error": f"partial output missing: {partial}",
            "partial_path": partial,
            "output_path": final_path,
        }
    checked = verify or verify_output(
        partial,
        expect_width=expect_width,
        expect_height=expect_height,
        expect_audio=expect_audio,
        expect_duration=expect_duration,
    )
    if not checked.get("ok"):
        return {
            "ok": False,
            "error": "output failed decode/moov check",
            "verify": checked,
            "partial_path": partial,
            "output_path": final_path,
        }
    dest_dir = os.path.dirname(os.path.abspath(final_path))
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    if os.path.normcase(os.path.abspath(partial)) != os.path.normcase(os.path.abspath(final_path)):
        os.replace(partial, final_path)
    sidecar = write_qa_sidecar(final_path, checked)
    return {
        "ok": True,
        "output_path": os.path.abspath(final_path),
        "qa_path": sidecar,
        "verify": checked,
        "md5": checked.get("md5"),
    }


def append_faststart(cmd: list[str], dest: str) -> list[str]:
    """Insert ``-movflags +faststart`` before *dest* when writing ISO-BMFF."""
    ext = os.path.splitext(dest)[1].lower()
    iso = ext in {".mp4", ".m4a", ".m4v", ".mov"} or dest.endswith(".mp4")
    if iso and "-movflags" not in cmd:
        return cmd + ["-movflags", "+faststart", dest]
    return cmd + [dest]


class InterruptedRender(FfmpegError):
    """ffmpeg was killed or otherwise did not finish the encode."""


class JobProcess:
    """One tracked ffmpeg subprocess for a render job."""

    def __init__(self) -> None:
        self.proc = None
        self.lock = threading.Lock()

    def run(self, args: list[str], *, timeout: float = 180, check: bool = True):
        from ..analysis.ffmpeg_util import spawn

        with self.lock:
            self.proc = spawn(args)
        try:
            stdout, stderr = self.proc.communicate(timeout=timeout)
        except Exception:
            self.kill()
            raise
        rc = self.proc.returncode
        if _returncode_interrupted(rc):
            raise InterruptedRender(
                f"{args[0]} interrupted (exit {rc})",
                stderr=stderr if isinstance(stderr, str) else "",
            )
        if check and rc != 0:
            err = stderr if isinstance(stderr, str) else (stderr or b"").decode("utf-8", "replace")
            raise FfmpegError(f"{args[0]} exited {rc}: {err[-800:]}", stderr=err)
        from subprocess import CompletedProcess

        return CompletedProcess(args, rc, stdout, stderr)

    def kill(self) -> None:
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except (OSError, ValueError, ProcessLookupError):
            pass
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    @property
    def pid(self) -> int | None:
        with self.lock:
            return None if self.proc is None else self.proc.pid


def _returncode_interrupted(rc: int | None) -> bool:
    if rc is None:
        return False
    # Negative = POSIX signal; 0xC000013A = Windows STATUS_CONTROL_C_EXIT.
    return rc < 0 or rc == 0xC000013A


def mux_to_partial(
    source: str,
    dest_partial: str,
    *,
    job: JobProcess | None = None,
    copy: bool = True,
    timeout: float = 180,
) -> None:
    """Remux *source* onto *dest_partial* with ``+faststart``."""
    parent = os.path.dirname(os.path.abspath(dest_partial))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        source,
    ]
    if copy:
        cmd.extend(["-c", "copy"])
    cmd = append_faststart(cmd, dest_partial)
    runner = job.run if job is not None else run
    runner(cmd, timeout=timeout)


def run_ffmpeg_atomic(
    cmd_without_output: list[str],
    final_path: str,
    *,
    timeout: float = 180,
    job: JobProcess | None = None,
    verify_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ffmpeg to ``<name>.partial<ext>``, verify, rename, write ``.qa.json``."""
    refused = check_output_path(final_path)
    if refused:
        return refused
    partial = partial_path(final_path)
    parent = os.path.dirname(os.path.abspath(partial))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = append_faststart(list(cmd_without_output), partial)
    try:
        runner = job.run if job is not None else run
        runner(cmd, timeout=timeout)
    except InterruptedRender as exc:
        return {
            "ok": False,
            "state": "interrupted",
            "error": str(exc),
            "partial_path": partial,
            "output_path": final_path,
        }
    except FfmpegError as exc:
        return {
            "ok": False,
            "state": "failed",
            "error": str(exc),
            "partial_path": partial,
            "output_path": final_path,
        }
    kwargs = {"expect_audio": False}
    kwargs.update(verify_kwargs or {})
    promoted = promote_partial(partial, final_path, **kwargs)
    if promoted.get("ok"):
        promoted["state"] = "complete"
    else:
        promoted.setdefault("state", "failed")
    return promoted


# Optional re-export so callers can place scratch next to work roots.
__all__ = [
    "InterruptedRender",
    "JobProcess",
    "append_faststart",
    "count_decode_errors",
    "file_md5",
    "final_path_from_partial",
    "has_moov_atom",
    "make_work_dir",
    "mux_to_partial",
    "partial_path",
    "promote_partial",
    "qa_path",
    "run_ffmpeg_atomic",
    "write_qa_sidecar",
]
