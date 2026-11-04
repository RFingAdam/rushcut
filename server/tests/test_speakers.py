"""Mic/mix speaker attribution (#28)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from rushcut.analysis.captions import generate_captions
from rushcut.analysis.probe import probe_media
from rushcut.analysis.speakers import attribute_speakers
from rushcut.profile import profile_from_data


def _run(ffmpeg_bin: str, args: list[str]) -> None:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _two_track_fixture(ffmpeg_bin: str, dest: Path) -> Path:
    """Speaker A on mic (+ bleed into mix at -12 dB, 20 ms). Speaker B mix-only."""
    _run(
        ffmpeg_bin,
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=1",
            "-filter_complex",
            (
                "[0:a][1:a]concat=n=2:v=0:a=1[mic];"
                "[0:a]adelay=20|20,volume=-12dB[bleed];"
                "[bleed][2:a]concat=n=2:v=0:a=1[mix]"
            ),
            "-map",
            "[mix]",
            "-map",
            "[mic]",
            "-metadata:s:a:0",
            "title=mix",
            "-metadata:s:a:1",
            "title=mic",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
    )
    return dest


def test_probe_lists_audio_streams_and_speech_ratio(tmp_path, ffmpeg_bin):
    dest = _two_track_fixture(ffmpeg_bin, tmp_path / "tracks.mkv")
    probe = probe_media(str(dest))
    assert probe["ok"] is True
    assert probe["audio_stream_count"] == 2
    titles = {str(s.get("title") or "") for s in probe["audio_streams"]}
    assert "mic" in titles and "mix" in titles
    for stream in probe["audio_streams"]:
        assert stream.get("channels")
        assert stream.get("speech_ratio") is not None


def test_attribute_speakers_mic_mix_and_delay(tmp_path, ffmpeg_bin):
    dest = _two_track_fixture(ffmpeg_bin, tmp_path / "session.mkv")
    words = [
        {"start_seconds": 0.15, "end_seconds": 0.7, "word": "hello"},
        {"start_seconds": 1.15, "end_seconds": 1.7, "word": "there"},
    ]
    result = attribute_speakers(str(dest), tracks={"mic": "mic", "mix": "mix"}, words=words)
    assert result["ok"] is True
    assert abs(float(result["delay_seconds"]) - 0.020) <= 0.005
    labels = [w["speaker"] for w in result["words"]]
    assert labels[0] == "host"
    assert labels[1] == "other"
    correct = sum(1 for got, want in zip(labels, ["host", "other"]) if got == want)
    assert correct / len(labels) >= 0.95


def test_single_track_unknown(color_clip):
    words = [{"start_seconds": 0.2, "end_seconds": 0.5, "word": "hi"}]
    result = attribute_speakers(str(color_clip), words=words)
    assert result["ok"] is True
    assert result["words"][0]["speaker"] == "unknown"
    assert "single-track" in (result.get("note") or "")


def test_profile_audio_tracks_and_caption_filter(tmp_path, ffmpeg_bin):
    dest = _two_track_fixture(ffmpeg_bin, tmp_path / "cap.mkv")
    profile = profile_from_data(
        {
            "audio": {"tracks": [{"role": "mix", "title": "mix"}, {"role": "mic", "title": "mic"}]},
            "lexicon": {"hotwords": ["Rushcut"]},
        }
    )
    assert profile.audio.tracks[0].role == "mix"
    words = [
        {"start_seconds": 0.15, "end_seconds": 0.5, "word": "hostline", "speaker": "host"},
        {"start_seconds": 1.15, "end_seconds": 1.5, "word": "otherline", "speaker": "other"},
    ]
    labeled = attribute_speakers(str(dest), words=words, profile=profile)
    assert labeled["ok"] is True
    caps = generate_captions(
        words=labeled["words"],
        output_dir=str(tmp_path),
        speakers=["host"],
        stem="host-only",
        max_words=1,
        extend_seconds=0,
    )
    assert caps["ok"] is True
    srt = Path(caps["srt_path"]).read_text(encoding="utf-8")
    assert "hostline" in srt
    assert "otherline" not in srt
    ass = Path(caps["ass_path"]).read_text(encoding="utf-8")
    assert "Style: Host," in ass
    assert "Dialogue: 0," in ass and ",Host,," in ass
    _ = dest
