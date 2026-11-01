from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rushcut.backends import reset_backend
from rushcut.host_client import VegasHostConfig
from rushcut.session import Session, reset_session


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    from rushcut.analysis.ffmpeg_util import which_ffmpeg

    return which_ffmpeg("ffmpeg")


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    return tmp_path


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


@pytest.fixture
def color_clip(media_dir: Path, ffmpeg_bin: str) -> Path:
    out = media_dir / "bars.mp4"
    _run_ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2.2:size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2.2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(out),
        ]
    )
    return out


@pytest.fixture
def scene_clip(media_dir: Path, ffmpeg_bin: str) -> Path:
    """Hard cut red → blue at 1.0s."""
    out = media_dir / "scenes.mp4"
    _run_ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:r=24:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=24:d=1.2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(out),
        ]
    )
    return out


@pytest.fixture
def spike_clip(media_dir: Path, ffmpeg_bin: str) -> Path:
    """Quiet tone, then a loud burst at ~1.2s."""
    out = media_dir / "spikes.mp4"
    _run_ffmpeg(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=320x180:r=24:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=8000:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=8000:duration=0.4",
            "-filter_complex",
            "[1:a]volume=0.05[quiet];[2:a]adelay=1200|1200,volume=1.0[loud];[quiet][loud]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )
    return out


class FakeHost:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.config = VegasHostConfig(transport="tcp", tcp_host="127.0.0.1", tcp_port=8752)

    def call(self, method: str, params=None):
        params = params or {}
        self.calls.append((method, params))
        return self.handler(method, params)


@pytest.fixture(autouse=True)
def _default_vegas_backend(monkeypatch):
    """Existing tests talk to the VEGAS session. Never attach to a live Resolve."""
    monkeypatch.delenv("DIRECTOR_EDITOR", raising=False)
    monkeypatch.delenv("VEGAS_DIRECTOR_EDITOR", raising=False)
    reset_backend()
    yield
    reset_backend()


@pytest.fixture
def fake_session(monkeypatch):
    def factory(handler, reachable: bool = True):
        host = FakeHost(handler)
        session = Session(client=host)
        if reachable:
            monkeypatch.setattr(session, "host_reachable", lambda: True)
            session.last_mode = "live"
        else:
            monkeypatch.setattr(session, "host_reachable", lambda: False)
        reset_session(session)
        reset_backend()
        return host, session

    yield factory
    reset_session()
    reset_backend()
