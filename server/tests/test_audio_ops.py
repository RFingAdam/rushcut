"""File-level audio ops: duck, bleep, insert/replace, loop fades."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from rushcut.analysis.audio_ops import (
    add_music_bed,
    bleep_spans,
    insert_audio,
    loop_fades,
    replace_audio_span,
)
from rushcut.analysis.pcm import decode_pcm, rms_db
from rushcut.edit.timeline import apply_timeline


def _wav(ffmpeg_bin: str, dest: Path, expr: str, duration: float, extra: list[str] | None = None) -> Path:
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        expr,
        "-t",
        f"{duration:.3f}",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    if extra:
        cmd[5:5] = extra
    subprocess.run(cmd, check=True, capture_output=True)
    return dest


def test_bleep_tone_inside_and_outside_unchanged(tmp_path, ffmpeg_bin):
    src = _wav(ffmpeg_bin, tmp_path / "src.wav", "sine=frequency=440:sample_rate=48000", 1.2)
    original, sr, ch = decode_pcm(str(src), sample_rate=48000, channels=2)
    spans = [{"start": 0.40, "end": 0.60, "word": "darn"}]
    out = tmp_path / "bleep.wav"
    result = bleep_spans(str(src), spans, output_path=str(out), mode="tone", freq=1000)
    assert result["ok"] is True, result
    got, _, _ = decode_pcm(str(out), sample_rate=sr, channels=ch)
    a = int(0.40 * sr) * ch
    b = int(0.60 * sr) * ch
    # Outside: sample-identical (PCM round-trip).
    assert original[:a] == got[:a]
    assert original[b:] == got[b:]
    # Inside: tone energy, not the original 440 Hz residual.
    inside_orig = rms_db(original, a, b)
    inside_new = rms_db(got, a, b)
    assert inside_new > -20.0
    # Mute mode: silence inside, outside unchanged.
    muted = tmp_path / "mute.wav"
    mute = bleep_spans(str(src), spans, output_path=str(muted), mode="mute")
    assert mute["ok"] is True
    silent, _, _ = decode_pcm(str(muted), sample_rate=sr, channels=ch)
    assert original[:a] == silent[:a]
    assert original[b:] == silent[b:]
    assert rms_db(silent, a, b) < -60.0
    assert inside_orig > -30.0


def test_bleep_accepts_masked_spans(tmp_path, ffmpeg_bin):
    src = _wav(ffmpeg_bin, tmp_path / "src.wav", "sine=frequency=330:sample_rate=48000", 0.8)
    result = apply_timeline(
        "bleep",
        path=str(src),
        output_path=str(tmp_path / "tl.wav"),
        spans=[{"start": 0.2, "end": 0.4, "word": "x"}],
        mode="mute",
    )
    assert result["ok"] is True, result
    assert result["effect"] == "bleep"


def test_insert_audio_report_and_replace_residual(tmp_path, ffmpeg_bin):
    bed = _wav(ffmpeg_bin, tmp_path / "bed.wav", "anoisesrc=color=pink:sample_rate=48000:amplitude=0.15", 2.5)
    vo = _wav(ffmpeg_bin, tmp_path / "vo.wav", "sine=frequency=880:sample_rate=48000", 0.6)
    mixed = tmp_path / "mixed.wav"
    inserted = insert_audio(
        str(bed),
        str(vo),
        at=1.0,
        output_path=str(mixed),
        duck_db=-6,
        ramp_ms=200,
        ramp_shape="raised_cosine",
    )
    assert inserted["ok"] is True, inserted
    assert abs(inserted["vo_start"] - 1.0) < 1e-6
    assert abs(inserted["vo_end"] - 1.6) < 1e-6
    assert inserted["duck_db"] == -6
    assert "in" in inserted["ramp_windows"] and "out" in inserted["ramp_windows"]
    assert abs(inserted["ramp_windows"]["in"]["start"] - 0.8) < 1e-6
    assert abs(inserted["ramp_windows"]["in"]["end"] - 1.0) < 1e-6
    assert abs(inserted["ramp_windows"]["out"]["start"] - 1.6) < 1e-6
    assert abs(inserted["ramp_windows"]["out"]["end"] - 1.8) < 1e-6

    removed = tmp_path / "removed.wav"
    replaced = replace_audio_span(
        str(mixed),
        str(vo),
        new_clip=None,
        output_path=str(removed),
        at=1.0,
        search_samples=64,
    )
    assert replaced["ok"] is True, replaced
    # Residual energy of the removed clip must be < -40 dB relative.
    mix, sr, ch = decode_pcm(str(removed), sample_rate=48000, channels=2)
    old, _, _ = decode_pcm(str(vo), sample_rate=sr, channels=ch)
    found = int(round(replaced["found_at"] * sr)) * ch
    n = min(len(old), len(mix) - found)
    old_e = sum(old[i] * old[i] for i in range(n)) or 1.0
    proj = sum(mix[found + i] * old[i] for i in range(n)) / old_e
    leftover = sum((old[i] * proj) ** 2 for i in range(n))
    rel_db = 10.0 * math.log10(leftover / old_e + 1e-18)
    assert rel_db < -40.0, f"residual {rel_db:.1f} dB (want < -40)"
    assert replaced["residual_db"] < -40.0


def test_music_bed_duck_depth(tmp_path, ffmpeg_bin):
    bed = _wav(ffmpeg_bin, tmp_path / "bed.wav", "sine=frequency=220:sample_rate=48000", 2.4)
    # Voice occupies 1.0–2.0 s.
    voice = tmp_path / "voice.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0:s=48000:d=2.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=900:sample_rate=48000:duration=1.0",
            "-filter_complex",
            "[1:a]adelay=1000|1000,volume=2.0[v];[0:a][v]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "[a]",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(voice),
        ],
        check=True,
        capture_output=True,
    )
    quiet = _wav(ffmpeg_bin, tmp_path / "quiet.wav", "aevalsrc=0:s=48000", 2.4)
    out = tmp_path / "ducked.wav"
    result = add_music_bed(
        str(bed),
        onto=str(quiet),
        output_path=str(out),
        gain=1.0,
        duck_under=str(voice),
        duck_db=-12,
        threshold=0.02,
        ratio=8,
        attack=10,
        release=50,
        fade_in=0.0,
        fade_out=0.0,
    )
    assert result["ok"] is True, result
    ducked, sr, ch = decode_pcm(str(out), sample_rate=48000, channels=2)
    # Rest region 0.2–0.7 vs ducked voice region 1.15–1.85.
    rest = rms_db(ducked, int(0.2 * sr) * ch, int(0.7 * sr) * ch)
    under = rms_db(ducked, int(1.15 * sr) * ch, int(1.85 * sr) * ch)
    depth = rest - under
    assert depth >= 12.0, f"duck depth {depth:.2f} dB (want >= 12)"


def test_music_bed_ducks_loud_transients(tmp_path, ffmpeg_bin):
    """Loud short transients under speech drop by ≥ the requested dB."""
    click = tmp_path / "clickbed.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=200:sample_rate=48000:duration=2.0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=2000:sample_rate=48000:duration=0.03",
            "-filter_complex",
            "[0:a]volume=0.25[q];[1:a]adelay=1200|1200,volume=4.0[c];[q][c]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "[a]",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(click),
        ],
        check=True,
        capture_output=True,
    )
    voice = tmp_path / "speech.wav"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:sample_rate=48000:duration=1.0",
            "-filter_complex",
            "[0:a]adelay=800|800,volume=2.0[a]",
            "-map",
            "[a]",
            "-t",
            "2.0",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(voice),
        ],
        check=True,
        capture_output=True,
    )
    raw, sr, ch = decode_pcm(str(click), sample_rate=48000, channels=2)
    click_a = int(1.20 * sr) * ch
    click_b = int(1.23 * sr) * ch
    raw_click = rms_db(raw, click_a, click_b)
    quiet = _wav(ffmpeg_bin, tmp_path / "q.wav", "aevalsrc=0:s=48000", 2.0)
    out = tmp_path / "t.wav"
    result = add_music_bed(
        str(click),
        onto=str(quiet),
        output_path=str(out),
        duck_under=str(voice),
        duck_db=-9,
        threshold=0.01,
        ratio=12,
        attack=5,
        release=40,
        fade_in=0,
        fade_out=0,
    )
    assert result["ok"] is True, result
    ducked, _, _ = decode_pcm(str(out), sample_rate=sr, channels=ch)
    new_click = rms_db(ducked, click_a, click_b)
    assert raw_click - new_click >= 9.0, f"transient duck {raw_click - new_click:.2f} dB"


def test_loop_fades_silence_and_black(tmp_path, ffmpeg_bin):
    src = tmp_path / "loop_src.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=160x90:r=25:d=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = tmp_path / "looped.mp4"
    result = loop_fades(str(src), in_s=0.2, out_s=0.2, output_path=str(out))
    assert result["ok"] is True, result
    assert result["in_s"] == 0.2
    samples, sr, ch = decode_pcm(str(out), sample_rate=48000, channels=2)
    head = rms_db(samples, 0, int(0.05 * sr) * ch)
    mid = rms_db(samples, int(0.5 * sr) * ch, int(0.7 * sr) * ch)
    tail = rms_db(samples, int(1.15 * sr) * ch, int(1.2 * sr) * ch)
    assert mid - head >= 12.0, f"head fade {head:.1f} vs mid {mid:.1f}"
    assert mid - tail >= 12.0, f"tail fade {tail:.1f} vs mid {mid:.1f}"
    # First and last frames should be near black (0.2 s fade to/from black).
    first = tmp_path / "first.rgb"
    last = tmp_path / "last.rgb"
    subprocess.run(
        [ffmpeg_bin, "-y", "-i", str(out), "-vf", "select=eq(n\\,0)", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", str(first)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [ffmpeg_bin, "-y", "-sseof", "-0.04", "-i", str(out), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", str(last)],
        check=True, capture_output=True,
    )
    for frame, name in ((first, "first"), (last, "last")):
        raw = frame.read_bytes()
        mean = sum(raw) / max(1, len(raw))
        assert mean < 45, f"{name} frame mean {mean:.1f} (want near black)"
