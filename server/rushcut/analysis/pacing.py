"""Narration pacing from Whisper word timestamps.

Words per minute, dead stretches (gaps of ``dead_threshold_s`` or more with
no transcribed word, counting the lead-in and the tail), the longest gap,
the words in the first 15 s, and optionally when the title's promise is
first spoken. Reuses :func:`rushcut.analysis.whisper.transcribe_media`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from typing import Any

from .probe import probe_media
from .whisper import transcribe_media

DEAD_THRESHOLD_S = 8.0
FIRST_WINDOW_S = 15.0
WPM_MIN_NARRATED = 110.0
PROMISE_BY_S = 15.0

_TOKEN_RE = re.compile(r"[^0-9a-z]+")


def _norm_token(text: str) -> str:
    return _TOKEN_RE.sub("", text.lower())


def _word_rows(words: list[dict[str, Any]]) -> list[tuple[float, float, str]]:
    """Accept transcribe_media words ({start_seconds, end_seconds, word}) or {s, e, w}."""
    rows: list[tuple[float, float, str]] = []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        start = w.get("start_seconds", w.get("s", w.get("start")))
        end = w.get("end_seconds", w.get("e", w.get("end", start)))
        text = str(w.get("word", w.get("w", w.get("text", ""))) or "").strip()
        try:
            start_f = float(start)
            end_f = float(end if end is not None else start)
        except (TypeError, ValueError):
            continue
        if not _norm_token(text):
            continue  # punctuation-only tokens are not words
        rows.append((start_f, max(start_f, end_f), text))
    rows.sort(key=lambda r: r[0])
    return rows


def _gap(start: float, end: float) -> dict[str, float]:
    return {"start_s": round(start, 2), "end_s": round(end, 2), "seconds": round(end - start, 2)}


def _find_promise(rows: list[tuple[float, float, str]], keywords: list[str]) -> dict[str, Any]:
    tokens = [_norm_token(t) for _, _, t in rows]
    best: dict[str, Any] = {"keywords": keywords, "at_s": None, "matched": None}
    for phrase in keywords:
        parts = [p for p in (_norm_token(x) for x in str(phrase).split()) if p]
        if not parts:
            continue
        for i in range(len(tokens) - len(parts) + 1):
            if tokens[i : i + len(parts)] == parts:
                at = rows[i][0]
                if best["at_s"] is None or at < best["at_s"]:
                    best = {"keywords": keywords, "at_s": round(at, 2), "matched": phrase}
                break
    return best


def pacing_from_words(
    words: list[dict[str, Any]],
    duration_seconds: float,
    dead_threshold_s: float = DEAD_THRESHOLD_S,
    first_window_s: float = FIRST_WINDOW_S,
    promise_keywords: list[str] | None = None,
    wpm_min: float | None = WPM_MIN_NARRATED,
    promise_by_s: float = PROMISE_BY_S,
) -> dict[str, Any]:
    """Pacing metrics from word timestamps (no model needed)."""
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "duration_seconds must be a number"}
    if duration <= 0:
        return {"ok": False, "error": "duration_seconds must be > 0"}
    if dead_threshold_s <= 0:
        return {"ok": False, "error": "dead_threshold_s must be > 0"}
    rows = _word_rows(words)

    dead: list[dict[str, float]] = []
    longest = _gap(0.0, 0.0)
    prev_end = 0.0
    for start, end, _ in rows:
        if start - prev_end > longest["seconds"]:
            longest = _gap(prev_end, start)
        if start - prev_end >= dead_threshold_s:
            dead.append(_gap(prev_end, start))
        prev_end = max(prev_end, end)
    if duration - prev_end > longest["seconds"]:
        longest = _gap(prev_end, duration)
    if duration - prev_end >= dead_threshold_s:
        dead.append(_gap(prev_end, duration))

    first = [t for s, _, t in rows if s < first_window_s]
    wpm = round(len(rows) / (duration / 60.0), 1)
    result: dict[str, Any] = {
        "ok": True,
        "duration_seconds": round(duration, 2),
        "word_count": len(rows),
        "wpm": wpm,
        "dead_threshold_s": dead_threshold_s,
        "dead_stretch_count": len(dead),
        "dead_seconds_total": round(sum(d["seconds"] for d in dead), 2),
        "dead_stretches": dead,
        "longest_gap": longest,
        "first_15s": {"window_s": first_window_s, "word_count": len(first), "text": " ".join(first)},
        "first_window": {"words": first, "text": " ".join(first), "window_s": first_window_s},
    }
    gate: dict[str, Any] = {
        "no_dead_stretches": {"pass": not dead, "value": len(dead),
                              "target": f"0 gaps of {dead_threshold_s:g} s or more"},
    }
    if wpm_min is not None:
        gate["wpm"] = {"pass": wpm >= wpm_min, "value": wpm, "target": f">= {wpm_min:g} wpm (narrated story)"}
    if promise_keywords:
        promise = _find_promise(rows, [str(k) for k in promise_keywords if str(k).strip()])
        result["promise"] = promise
        at = promise["at_s"]
        result["keyword_at_s"] = at
        gate["promise_by_15s"] = {"pass": at is not None and at <= promise_by_s, "value": at,
                                  "target": f"title promise spoken by {promise_by_s:g} s"}
    checks = {k: v for k, v in gate.items() if isinstance(v, dict) and "pass" in v}
    reasons = [f"{name}: {item.get('target')}" for name, item in checks.items() if not item.get("pass")]
    passed = all(item["pass"] for item in checks.values())
    gate["passed"] = passed
    gate["reasons"] = reasons
    result["gate"] = gate
    result["gate_passed"] = passed
    return result


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cache_dir() -> str:
    override = os.environ.get("RUSHCUT_TRANSCRIPT_CACHE")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    path = os.path.join(tempfile.gettempdir(), "rushcut-transcripts")
    os.makedirs(path, exist_ok=True)
    return path


def _cache_name(path: str, model_size: str, language: str | None) -> str:
    content = _file_hash(path)
    located = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]
    return f"{content}-{located}-{model_size}-{language or 'auto'}.json"


def _load_cached_transcript(path: str, model_size: str, language: str | None) -> dict[str, Any] | None:
    cache = os.path.join(_cache_dir(), _cache_name(path, model_size, language))
    if not os.path.isfile(cache):
        return None
    try:
        with open(cache, encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("ok"):
        data["cached"] = True
        return data
    return None


def _store_cached_transcript(path: str, model_size: str, language: str | None, transcript: dict[str, Any]) -> None:
    if not transcript.get("ok"):
        return
    cache = os.path.join(_cache_dir(), _cache_name(path, model_size, language))
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(transcript, fh)
    except OSError:
        pass


def measure_pacing(
    media_path: str,
    dead_threshold_s: float = DEAD_THRESHOLD_S,
    promise_keywords: list[str] | None = None,
    model_size: str = "base",
    language: str | None = "en",
    wpm_min: float | None = WPM_MIN_NARRATED,
    keywords: list[str] | None = None,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Transcribe ``media_path`` with local Whisper and return pacing metrics.

    Soft-fails like ``transcribe_media`` when faster-whisper is missing. Use
    ``model_size="large-v3"`` for numbers comparable to published targets;
    smaller models drop words and overstate dead stretches. Transcripts are
    cached by SHA-256 of the file. ``keywords`` is an alias for
    ``promise_keywords``. Pass ``words`` to skip Whisper.
    """
    if not media_path:
        return {"ok": False, "error": "media_path is required"}
    if not os.path.isfile(media_path):
        return {"ok": False, "error": f"file not found: {media_path}"}
    try:
        threshold = float(dead_threshold_s)
    except (TypeError, ValueError):
        return {"ok": False, "error": "dead_threshold_s must be a number"}
    if threshold <= 0:
        return {"ok": False, "error": "dead_threshold_s must be > 0"}
    if promise_keywords is None:
        promise_keywords = keywords

    cached = False
    if words is not None:
        transcript = {"ok": True, "path": os.path.abspath(media_path), "words": words, "model": model_size}
    else:
        transcript = _load_cached_transcript(media_path, model_size, language)
        if transcript is None:
            transcript = transcribe_media(media_path, model_size=model_size, language=language)
            if transcript.get("ok"):
                _store_cached_transcript(media_path, model_size, language, transcript)
        else:
            cached = True
    if not transcript.get("ok"):
        return {**transcript, "stage": "transcribe"}
    words = transcript.get("words") or []

    duration = None
    source = None
    probe = probe_media(media_path)
    if probe.get("ok") and probe.get("duration_seconds"):
        duration, source = float(probe["duration_seconds"]), "ffprobe"
    elif transcript.get("duration_seconds"):
        duration, source = float(transcript["duration_seconds"]), "whisper"
    else:
        ends = [float(w.get("end_seconds") or 0) for w in words if isinstance(w, dict)]
        if ends and max(ends) > 0:
            duration, source = max(ends), "last_word"
    if not duration:
        return {"ok": False, "error": "could not determine the media duration"}

    result = pacing_from_words(
        words,
        duration,
        dead_threshold_s=threshold,
        promise_keywords=promise_keywords,
        wpm_min=wpm_min,
    )
    if result.get("ok"):
        result.update(
            {
                "path": transcript.get("path") or os.path.abspath(media_path),
                "duration_source": source,
                "model": transcript.get("model", model_size),
                "device": transcript.get("device"),
                "language": transcript.get("language", language),
                "cached": cached,
            }
        )
    return result
