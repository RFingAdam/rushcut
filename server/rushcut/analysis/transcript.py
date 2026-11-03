"""Generic transcription analysis: word times, speech spans, optional speakers.

Usable by any edit profile or director plugin. Does not rank or select clips.
Speaker labels are generic ids (SPEAKER_L / SPEAKER_R / SPEAKER_00), not
roles. Mixed-track mic-vs-mix attribution is a separate issue (#28).
"""
from __future__ import annotations

import os
import struct
import tempfile
from typing import Any

from .ffmpeg_util import FfmpegError, run, which_ffmpeg
from .probe import probe_media
from .whisper import transcribe_media

_START_KEYS = ("start_seconds", "start", "in_seconds", "in", "source_in")
_END_KEYS = ("end_seconds", "end", "out_seconds", "out", "source_out")


def _first_num(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def word_times(item: dict[str, Any]) -> tuple[float, float] | None:
    start = _first_num(item, _START_KEYS)
    end = _first_num(item, _END_KEYS)
    if start is None:
        return None
    if end is None:
        length = _first_num(item, ("length_seconds", "duration_seconds", "length"))
        if length is None:
            return None
        end = start + length
    if end < start:
        start, end = end, start
    return start, end


def speech_spans_from_words(
    words: list[dict[str, Any]] | None,
    merge_gap_seconds: float = 0.3,
) -> list[dict[str, Any]]:
    """Merge nearby words into speech regions. Gap > merge_gap starts a new span."""
    timed: list[tuple[float, float, dict[str, Any]]] = []
    for word in words or []:
        pair = word_times(word)
        if pair is None:
            continue
        timed.append((pair[0], pair[1], word))
    timed.sort(key=lambda t: t[0])
    spans: list[dict[str, Any]] = []
    for start, end, word in timed:
        if spans and start - float(spans[-1]["end_seconds"]) <= merge_gap_seconds:
            spans[-1]["end_seconds"] = round(max(float(spans[-1]["end_seconds"]), end), 3)
            spans[-1]["word_count"] = int(spans[-1]["word_count"]) + 1
            text = (word.get("word") or word.get("text") or "").strip()
            if text:
                spans[-1]["text"] = (spans[-1].get("text") or "") + " " + text
        else:
            text = (word.get("word") or word.get("text") or "").strip()
            spans.append(
                {
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "word_count": 1,
                    "text": text,
                }
            )
    for span in spans:
        span["text"] = " ".join(str(span.get("text") or "").split())
    return spans


def attach_speakers(
    words: list[dict[str, Any]] | None,
    turns: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Copy speaker from overlapping diarization turns onto each word."""
    labeled: list[dict[str, Any]] = []
    ranges: list[tuple[float, float, str]] = []
    for turn in turns or []:
        pair = word_times(turn)
        speaker = turn.get("speaker")
        if pair is None or speaker is None or speaker == "":
            continue
        ranges.append((pair[0], pair[1], str(speaker)))
    ranges.sort(key=lambda t: t[0])
    for word in words or []:
        item = dict(word)
        pair = word_times(item)
        if pair is None:
            labeled.append(item)
            continue
        mid = (pair[0] + pair[1]) / 2.0
        speaker = None
        for start, end, label in ranges:
            if start <= mid < end or (start == end and abs(mid - start) < 1e-6):
                speaker = label
                break
        if speaker is not None:
            item["speaker"] = speaker
        labeled.append(item)
    return labeled


def _decode_pcm(path: str, channels: int, sample_rate: int = 8000) -> tuple[bytes, int, int]:
    tmp = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    tmp.close()
    out = tmp.name
    try:
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                path,
                "-ac",
                str(channels),
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                out,
            ],
            timeout=180,
        )
        with open(out, "rb") as fh:
            return fh.read(), sample_rate, channels
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _rms_s16(chunk: bytes) -> float:
    if len(chunk) < 2:
        return 0.0
    n = len(chunk) // 2
    total = 0
    for (sample,) in struct.iter_unpack("<h", chunk[: n * 2]):
        total += sample * sample
    return (total / n) ** 0.5 if n else 0.0


def speech_spans_from_audio(
    path: str,
    window_seconds: float = 0.08,
    merge_gap_seconds: float = 0.2,
    min_span_seconds: float = 0.08,
) -> dict[str, Any]:
    """Energy-based speech regions. No model; used when a transcript is missing."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        pcm, sr, _ch = _decode_pcm(path, channels=1)
    except FfmpegError as exc:
        return {"ok": False, "error": str(exc)}
    if not pcm:
        return {"ok": False, "error": "decoded audio was empty"}
    hop = max(1, int(sr * window_seconds))
    width = 2
    rms_vals: list[tuple[float, float]] = []
    n = len(pcm) // width
    for i in range(0, n, hop):
        chunk = pcm[i * width : (i + hop) * width]
        if len(chunk) < width * 4:
            continue
        rms_vals.append((i / sr, _rms_s16(chunk)))
    if not rms_vals:
        return {"ok": False, "error": "no analysis windows"}
    ranked = sorted(r for _t, r in rms_vals)
    noise = ranked[max(0, len(ranked) // 5)]
    threshold = max(noise * 3.0, 200.0)
    active: list[tuple[float, float]] = []
    for t, rms in rms_vals:
        if rms >= threshold:
            active.append((t, t + window_seconds))
    spans: list[dict[str, Any]] = []
    for start, end in active:
        if spans and start - float(spans[-1]["end_seconds"]) <= merge_gap_seconds:
            spans[-1]["end_seconds"] = round(end, 3)
        else:
            spans.append({"start_seconds": round(start, 3), "end_seconds": round(end, 3)})
    spans = [s for s in spans if float(s["end_seconds"]) - float(s["start_seconds"]) >= min_span_seconds]
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "method": "energy",
        "threshold_rms": round(threshold, 2),
        "spans": spans,
    }


def diarize_stereo(path: str, words: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Label each word from L/R RMS. Generic channel ids, not people."""
    if not path:
        return {"ok": False, "available": False, "error": "path is required", "turns": []}
    if not os.path.isfile(path):
        return {"ok": False, "available": False, "error": f"file not found: {path}", "turns": []}
    probe = probe_media(path)
    channels = int(probe.get("channels") or 0) if probe.get("ok") else 0
    if channels < 2:
        return {
            "ok": True,
            "available": False,
            "method": "stereo",
            "error": "stereo diarization needs at least 2 audio channels",
            "turns": [],
            "words": list(words or []),
        }
    try:
        pcm, sr, ch = _decode_pcm(path, channels=2)
    except FfmpegError as exc:
        return {"ok": False, "available": False, "error": str(exc), "turns": []}
    frame = ch  # interleaved s16 samples per frame
    labeled = []
    turns: list[dict[str, Any]] = []
    for word in words or []:
        item = dict(word)
        pair = word_times(item)
        if pair is None:
            labeled.append(item)
            continue
        start_i = max(0, int(pair[0] * sr))
        end_i = max(start_i + 1, int(pair[1] * sr))
        left = bytearray()
        right = bytearray()
        for i in range(start_i, min(end_i, len(pcm) // (2 * frame))):
            off = i * 2 * frame
            left.extend(pcm[off : off + 2])
            right.extend(pcm[off + 2 : off + 4])
        l_rms = _rms_s16(bytes(left))
        r_rms = _rms_s16(bytes(right))
        if l_rms > r_rms * 1.4 and l_rms > 80:
            speaker = "SPEAKER_L"
        elif r_rms > l_rms * 1.4 and r_rms > 80:
            speaker = "SPEAKER_R"
        elif l_rms > 80 or r_rms > 80:
            speaker = "SPEAKER_BOTH"
        else:
            speaker = "SPEAKER_NONE"
        item["speaker"] = speaker
        item["channel_rms"] = {"L": round(l_rms, 1), "R": round(r_rms, 1)}
        labeled.append(item)
        turns.append(
            {
                "start_seconds": round(pair[0], 3),
                "end_seconds": round(pair[1], 3),
                "speaker": speaker,
            }
        )
    return {
        "ok": True,
        "available": True,
        "method": "stereo",
        "turns": turns,
        "words": labeled,
    }


def diarize_pyannote(path: str) -> dict[str, Any]:
    """Optional neural diarization. Soft-fails if pyannote.audio is missing."""
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError:
        return {
            "ok": True,
            "available": False,
            "method": "pyannote",
            "error": (
                "pyannote.audio is not installed. Pass turns=... to attach_speakers, "
                "use diarize='stereo' on a 2-channel file, or install pyannote.audio."
            ),
            "turns": [],
        }
    token = os.environ.get("PYANNOTE_AUTH_TOKEN") or os.environ.get("HF_TOKEN")
    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=token or True,
        )
        diarization = pipeline(path)
    except Exception as exc:
        return {
            "ok": False,
            "available": True,
            "method": "pyannote",
            "error": f"pyannote failed: {exc}",
            "turns": [],
        }
    turns: list[dict[str, Any]] = []
    try:
        for turn, _track, speaker in diarization.itertracks(yield_label=True):
            turns.append(
                {
                    "start_seconds": round(float(turn.start), 3),
                    "end_seconds": round(float(turn.end), 3),
                    "speaker": str(speaker),
                }
            )
    except Exception as exc:
        return {
            "ok": False,
            "available": True,
            "method": "pyannote",
            "error": f"pyannote parse failed: {exc}",
            "turns": [],
        }
    return {"ok": True, "available": True, "method": "pyannote", "turns": turns}


def diarize_media(
    path: str | None = None,
    words: list[dict[str, Any]] | None = None,
    turns: list[dict[str, Any]] | None = None,
    method: bool | str = "auto",
) -> dict[str, Any]:
    """Optional speaker diarization. method: auto | stereo | pyannote | off | True/False."""
    if turns:
        labeled = attach_speakers(words, turns)
        return {
            "ok": True,
            "available": True,
            "method": "turns",
            "turns": turns,
            "words": labeled,
        }
    flag = str(method).strip().lower() if not isinstance(method, bool) else ("auto" if method else "off")
    if flag in {"", "0", "false", "off", "none", "no"}:
        return {
            "ok": True,
            "available": False,
            "method": "off",
            "turns": [],
            "words": list(words or []),
        }
    if flag in {"true", "1", "yes"}:
        flag = "auto"
    stereo: dict[str, Any] = {}
    if path and flag in {"auto", "stereo"}:
        stereo = diarize_stereo(path, words)
        if stereo.get("available"):
            return stereo
        if flag == "stereo":
            return stereo
    if flag in {"auto", "pyannote"}:
        if not path:
            return {
                "ok": True,
                "available": False,
                "method": "pyannote",
                "error": "path is required for pyannote diarization",
                "turns": [],
                "words": list(words or []),
            }
        neural = diarize_pyannote(path)
        if neural.get("available") and neural.get("ok") and neural.get("turns"):
            labeled = attach_speakers(words, neural["turns"])
            neural["words"] = labeled
            return neural
        if flag == "pyannote":
            neural["words"] = list(words or [])
            return neural
        # auto: fall through with a note that nothing ran
        note = stereo.get("error") if path else "no diarization method available"
        if neural.get("error"):
            note = neural["error"]
        return {
            "ok": True,
            "available": False,
            "method": "auto",
            "error": note,
            "turns": [],
            "words": list(words or []),
        }
    return {
        "ok": False,
        "available": False,
        "error": f"unknown diarize method: {method!r}",
        "turns": [],
        "words": list(words or []),
    }


def analyze_transcript(
    path: str | None = None,
    *,
    words: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    turns: list[dict[str, Any]] | None = None,
    model_size: str = "base",
    language: str | None = "en",
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    diarize: bool | str = False,
    text: str | None = None,
    offset_map: list[dict[str, Any]] | None = None,
    session_start: float | None = None,
    session_end: float | None = None,
    session_seconds: float | None = None,
    initial_prompt: str | None = None,
) -> dict[str, Any]:
    """Transcription/analysis primitive: word timestamps + optional speakers.

    Supply `words` to skip Whisper (tests and cached transcripts). Otherwise
    `path` is transcribed with faster-whisper (soft-fail if missing).
    Session time (from ``stitch_session``) maps onto the source file.
    """
    if offset_map is not None and (session_seconds is not None or session_start is not None):
        from .session import lookup_session_time

        t0 = session_start if session_start is not None else session_seconds
        hit = lookup_session_time(offset_map, float(t0))
        if not hit.get("ok"):
            return hit
        path = str(hit["path"])
        start_seconds = float(hit["file_seconds"])
        t1 = session_end if session_end is not None else None
        if t1 is not None:
            end_hit = lookup_session_time(offset_map, float(t1))
            if end_hit.get("ok") and end_hit.get("path") == path:
                end_seconds = float(end_hit["file_seconds"])
    source: dict[str, Any]
    if words is not None:
        source = {
            "ok": True,
            "available": True,
            "path": os.path.abspath(path) if path else None,
            "text": text if text is not None else " ".join(
                str(w.get("word") or w.get("text") or "") for w in words
            ).strip(),
            "segments": list(segments or []),
            "words": [dict(w) for w in words],
            "model": None,
        }
        if start_seconds is not None:
            source["start_seconds"] = float(start_seconds)
        if end_seconds is not None:
            source["end_seconds"] = float(end_seconds)
    else:
        if not path:
            return {"ok": False, "error": "path or words is required"}
        source = transcribe_media(
            path,
            model_size=model_size,
            language=language,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            initial_prompt=initial_prompt,
        )
        if not source.get("ok"):
            return source

    word_list = list(source.get("words") or [])
    diar = diarize_media(path, words=word_list, turns=turns, method=diarize)
    if diar.get("words"):
        word_list = list(diar["words"])
    source["words"] = word_list
    source["speech_spans"] = speech_spans_from_words(word_list)
    speakers = sorted({str(w.get("speaker")) for w in word_list if w.get("speaker")})
    source["speakers"] = speakers
    source["diarization"] = {
        "available": bool(diar.get("available")),
        "method": diar.get("method"),
        "turns": diar.get("turns") or [],
        "error": diar.get("error"),
    }
    source["ok"] = True
    return source
