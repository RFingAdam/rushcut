"""Score cut seams against the mic envelope and snap them to the nearest dip."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from ..analysis.audio import envelope_from_media
from ..analysis.transcript import word_times
from .speech import coverage_ranges_from_cuts, cut_points_from_cuts

VOICED_THRESHOLD_DB = -32.0
WINDOW_MS = 300.0
DEFAULT_FADE_SECONDS = 0.04
DEFAULT_DBFS = -96.0
_NEAR_SAMPLE_SECONDS = 0.15

_START_KEYS = ("start_seconds", "start", "in_seconds", "in", "source_in")
_END_KEYS = ("end_seconds", "end", "out_seconds", "out", "source_out")
_LENGTH_KEYS = ("length_seconds", "duration_seconds", "length")
_DB_KEYS = ("dbfs", "db", "dB", "level_db", "envelope_db")
_TIME_KEYS = ("at_seconds", "t", "seconds", "time", "start_seconds")


def _num(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def _set_num(item: dict[str, Any], keys: tuple[str, ...], value: float) -> None:
    rounded = round(float(value), 4)
    for key in keys:
        if key in item:
            item[key] = rounded
            return
    item[keys[0]] = rounded


def _words_list(words: Any) -> list[dict[str, Any]]:
    if words is None:
        return []
    if isinstance(words, dict):
        return list(words.get("words") or words.get("tokens") or [])
    return [w for w in words if isinstance(w, dict)]


def _segment_dicts(cuts: Any) -> list[dict[str, Any]]:
    if cuts is None:
        return []
    if isinstance(cuts, dict):
        found: list[dict[str, Any]] = []
        if "cuts" in cuts:
            found.extend(_segment_dicts(cuts.get("cuts")))
        for key in ("segments", "events", "clips"):
            if key in cuts:
                found.extend(_segment_dicts(cuts.get(key)))
        if found:
            return found
        if _num(cuts, _START_KEYS) is not None:
            return [cuts]
        return []
    out: list[dict[str, Any]] = []
    for item in cuts:
        if isinstance(item, dict) and _num(item, _START_KEYS) is not None:
            out.append(item)
    return out


def _segment_end(item: dict[str, Any]) -> float | None:
    end = _num(item, _END_KEYS)
    if end is not None:
        return end
    start = _num(item, _START_KEYS)
    length = _num(item, _LENGTH_KEYS)
    if start is not None and length is not None:
        return start + length
    return None


def total_kept_seconds(cuts: Any) -> float:
    """Sum of kept [in, out) lengths."""
    ranges = coverage_ranges_from_cuts(cuts)
    return round(sum(max(0.0, b - a) for a, b in ranges), 6)


def _fade_seconds(point: dict[str, Any], default: float = DEFAULT_FADE_SECONDS) -> float:
    role = str(point.get("role") or "cut")
    keys = ("fade_seconds", "fade_s", "fade")
    if role == "in":
        keys = ("fade_in_seconds", "fade_in", "fade_seconds", "fade_s", "fade")
    elif role == "out":
        keys = ("fade_out_seconds", "fade_out", "fade_seconds", "fade_s", "fade")
    for key in keys:
        if key in point and point[key] is not None:
            try:
                return float(point[key])
            except (TypeError, ValueError):
                continue
    fade_ms = point.get("fade_ms") or point.get("fade_length_ms")
    if fade_ms is not None:
        try:
            return float(fade_ms) / 1000.0
        except (TypeError, ValueError):
            pass
    return float(default)


def _sample_time(item: dict[str, Any]) -> float | None:
    return _num(item, _TIME_KEYS)


def _sample_dbfs(item: dict[str, Any]) -> float | None:
    db = _num(item, _DB_KEYS)
    if db is not None:
        return db
    rms = item.get("rms")
    if rms is not None:
        from ..analysis.audio import pcm_dbfs

        try:
            return pcm_dbfs(float(rms))
        except (TypeError, ValueError):
            return None
    return None


def _normalize_samples(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        for key in ("samples", "points", "windows", "envelope", "levels"):
            if key in raw:
                return _normalize_samples(raw.get(key))
        if _sample_time(raw) is not None:
            raw = [raw]
        else:
            return []
    samples: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                samples.append({"at_seconds": float(item[0]), "dbfs": float(item[1])})
            except (TypeError, ValueError):
                continue
            continue
        if not isinstance(item, dict):
            continue
        t = _sample_time(item)
        db = _sample_dbfs(item)
        if t is None or db is None:
            continue
        samples.append({"at_seconds": float(t), "dbfs": float(db)})
    samples.sort(key=lambda s: s["at_seconds"])
    return samples


def load_envelope(
    envelope: Any = None,
    mic_audio: Any = None,
    *,
    hop_seconds: float = 0.01,
) -> list[dict[str, Any]]:
    """Accept a sample list, JSON path, or mic audio path."""
    if envelope is not None:
        if isinstance(envelope, (str, os.PathLike)):
            path = Path(envelope)
            if path.is_file():
                suffix = path.suffix.lower()
                if suffix in {".json", ".yaml", ".yml"}:
                    text = path.read_text(encoding="utf-8")
                    if suffix == ".json":
                        return _normalize_samples(json.loads(text))
                    import yaml

                    return _normalize_samples(yaml.safe_load(text))
                loaded = envelope_from_media(str(path), hop_seconds=hop_seconds)
                return list(loaded.get("samples") or [])
            return []
        samples = _normalize_samples(envelope)
        if samples:
            return samples
    if mic_audio:
        if isinstance(mic_audio, (str, os.PathLike)):
            loaded = envelope_from_media(str(mic_audio), hop_seconds=hop_seconds)
            return list(loaded.get("samples") or [])
        return _normalize_samples(mic_audio)
    return []


def dbfs_at(
    samples: list[dict[str, Any]],
    t: float,
    *,
    default: float = DEFAULT_DBFS,
    near_seconds: float = _NEAR_SAMPLE_SECONDS,
) -> float:
    """Nearest-sample dBFS. Isolated samples do not leak across the timeline."""
    if not samples:
        return float(default)
    best = min(samples, key=lambda s: abs(float(s["at_seconds"]) - t))
    if abs(float(best["at_seconds"]) - t) > near_seconds:
        return float(default)
    return float(best["dbfs"])


def _local_minimum(
    samples: list[dict[str, Any]],
    t: float,
    window_seconds: float,
    *,
    default: float = DEFAULT_DBFS,
) -> tuple[float, float]:
    """Quietest sample in ±window. Ties break toward the original cut."""
    lo, hi = t - window_seconds, t + window_seconds
    nearby = [s for s in samples if lo <= float(s["at_seconds"]) <= hi]
    if not nearby:
        return t, dbfs_at(samples, t, default=default)
    # Prefer a real dip: lowest dBFS, then closest to t.
    best = min(nearby, key=lambda s: (float(s["dbfs"]), abs(float(s["at_seconds"]) - t)))
    return float(best["at_seconds"]), float(best["dbfs"])


def _word_at(words: list[dict[str, Any]], t: float) -> dict[str, Any] | None:
    for word in words:
        pair = word_times(word)
        if pair is None:
            continue
        start, end = pair
        if start < t < end:
            return word
    return None


def _word_timestamp_inside(word: dict[str, Any], start: float, end: float) -> bool:
    pair = word_times(word)
    if pair is None:
        return False
    ws, we = pair
    if start <= ws < end:
        return True
    mid = (ws + we) / 2.0
    return start <= mid < end


def _words_inside(words: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    return [w for w in words if _word_timestamp_inside(w, start, end)]


def _covers_words(start: float, end: float, words: list[dict[str, Any]]) -> bool:
    return all(_word_timestamp_inside(w, start, end) for w in words)


def _segment_bounds_for_point(
    cuts: Any,
    point: dict[str, Any],
) -> tuple[float, float] | None:
    idx = point.get("segment_index")
    segments = _segment_dicts(cuts)
    if idx is not None and 0 <= int(idx) < len(segments):
        item = segments[int(idx)]
        start = _num(item, _START_KEYS)
        end = _segment_end(item)
        if start is not None and end is not None:
            return float(start), float(end)
    return None


def _safe_snap_time(
    *,
    cuts: Any,
    point: dict[str, Any],
    suggested: float,
    words: list[dict[str, Any]],
    keep_words: bool,
    max_shift_ms: float | None,
) -> tuple[float | None, str | None]:
    t = float(point["at_seconds"])
    shift_ms = abs(suggested - t) * 1000.0
    if max_shift_ms is not None and shift_ms > float(max_shift_ms) + 1e-6:
        return None, "max_shift_ms"
    if not keep_words or not words:
        return suggested, None
    bounds = _segment_bounds_for_point(cuts, point)
    if bounds is None:
        return suggested, None
    start, end = bounds
    kept = _words_inside(words, start, end)
    role = str(point.get("role") or "cut")
    new_start, new_end = start, end
    if role == "in":
        new_start = suggested
    elif role == "out":
        new_end = suggested
    else:
        return suggested, None
    if new_end < new_start:
        return None, "inverted"
    if _covers_words(new_start, new_end, kept):
        return suggested, None
    # Clamp so every originally-covered word timestamp stays inside.
    if role == "in" and kept:
        limit = min(float(word_times(w)[0]) for w in kept)  # type: ignore[index]
        clamped = min(suggested, limit)
        if clamped + 1e-6 >= end:
            return None, "keep_words"
        if _covers_words(clamped, new_end, kept):
            if max_shift_ms is not None and abs(clamped - t) * 1000.0 > float(max_shift_ms) + 1e-6:
                return None, "max_shift_ms"
            return clamped, None
        return None, "keep_words"
    if role == "out" and kept:
        limit = max(float(word_times(w)[1]) for w in kept)  # type: ignore[index]
        clamped = max(suggested, limit)
        if clamped - 1e-6 <= start:
            return None, "keep_words"
        if _covers_words(new_start, clamped, kept):
            if max_shift_ms is not None and abs(clamped - t) * 1000.0 > float(max_shift_ms) + 1e-6:
                return None, "max_shift_ms"
            return clamped, None
        return None, "keep_words"
    return None, "keep_words"


def _listen_here(t: float, role: str | None, reason: str | None) -> str:
    label = f"{role} " if role else ""
    extra = f" ({reason})" if reason else ""
    return f"listen here {t:.3f}s {label}source{extra}".rstrip()


def format_seams_markdown(result: dict[str, Any]) -> str:
    """Markdown table of flagged seams plus a listen-here list."""
    seams = list(result.get("seams") or [])
    flagged = [s for s in seams if s.get("voiced") or s.get("mid_word") or s.get("unresolvable")]
    lines = [
        (
            f"seams: {len(flagged)} flagged / {len(seams)} total "
            f"(voiced>{result.get('voiced_threshold_db', VOICED_THRESHOLD_DB):g} dBFS)"
        ),
        "",
        "| # | role | t | dBFS | min t | min dBFS | voiced | mid-word | fade ms | suggestion |",
        "|---|------|---|------|-------|----------|--------|----------|---------|------------|",
    ]
    if not flagged:
        lines.append("| — | — | — | — | — | — | — | — | — | all seams in a gap |")
    for seam in flagged:
        sug = seam.get("suggestion") or {}
        sug_txt = "—"
        if sug.get("at_seconds") is not None:
            apply = "snap" if sug.get("apply") else "blocked"
            sug_txt = f"{apply} {float(sug['at_seconds']):.3f}s ({sug.get('shift_ms', 0):.0f} ms)"
            if sug.get("blocked"):
                sug_txt += f" [{sug['blocked']}]"
        lines.append(
            "| {idx} | {role} | {t:.3f} | {db:.1f} | {mt:.3f} | {mdb:.1f} | {voiced} | {mid} | {fade:.0f} | {sug} |".format(
                idx=seam.get("index", ""),
                role=seam.get("role") or "cut",
                t=float(seam.get("at_seconds") or 0),
                db=float(seam.get("dbfs") if seam.get("dbfs") is not None else DEFAULT_DBFS),
                mt=float(seam.get("local_min_seconds") if seam.get("local_min_seconds") is not None else seam.get("at_seconds") or 0),
                mdb=float(seam.get("local_min_dbfs") if seam.get("local_min_dbfs") is not None else DEFAULT_DBFS),
                voiced="yes" if seam.get("voiced") else "no",
                mid="yes" if seam.get("mid_word") else "no",
                fade=float(seam.get("fade_seconds") or 0) * 1000.0,
                sug=sug_txt,
            )
        )
    listen = list(result.get("listen_here") or [])
    if listen:
        lines.extend(["", "Unresolvable voiced seams:"])
        for item in listen:
            if isinstance(item, dict):
                t = item.get("at_seconds")
                role = item.get("role") or "cut"
                why = item.get("reason") or "voiced"
                lines.append(f"- listen here {float(t):.3f}s ({role}, source) — {why}")
            else:
                lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def check_seams(
    cuts: Any,
    mic_audio: Any = None,
    envelope: Any = None,
    words: Any = None,
    *,
    window_ms: float = WINDOW_MS,
    voiced_threshold_db: float = VOICED_THRESHOLD_DB,
    fade_seconds: float = DEFAULT_FADE_SECONDS,
    keep_words: bool = True,
    max_shift_ms: float | None = None,
    mode: str = "fail",
    hop_seconds: float = 0.01,
) -> dict[str, Any]:
    """Score every cut against the mic envelope and word timestamps.

    Per seam: envelope dBFS at the cut, the local minimum within ± window,
    mid-word flag, voiced flag (envelope > threshold), fade length, and a
    suggestion toward the nearest dip.
    """
    if str(mode).lower() == "off":
        return {
            "id": "seams",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "seams": [],
            "listen_here": [],
            "markdown": "",
            "value": 0,
            "limit": 0,
        }
    samples = load_envelope(envelope, mic_audio, hop_seconds=hop_seconds)
    word_list = _words_list(words)
    points = cut_points_from_cuts(cuts)
    window_s = max(0.0, float(window_ms) / 1000.0)
    threshold = float(voiced_threshold_db)
    if max_shift_ms is None:
        max_shift_ms = float(window_ms)
    seams: list[dict[str, Any]] = []
    listen_here: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        t = float(point["at_seconds"])
        db = dbfs_at(samples, t)
        min_t, min_db = _local_minimum(samples, t, window_s)
        mid_word_hit = _word_at(word_list, t)
        mid_word = mid_word_hit is not None
        voiced = db > threshold
        fade = _fade_seconds(point, fade_seconds)
        suggestion_t = min_t
        reason = "voiced" if voiced else ("mid_word" if mid_word else "gap")
        apply = False
        blocked = None
        unresolvable = False
        already_quiet = (not voiced) and (not mid_word)
        if already_quiet:
            suggestion_t = t
            reason = "unchanged"
        else:
            safe, blocked = _safe_snap_time(
                cuts=cuts,
                point=point,
                suggested=suggestion_t,
                words=word_list,
                keep_words=keep_words,
                max_shift_ms=max_shift_ms,
            )
            if safe is None:
                unresolvable = True
            else:
                suggestion_t = safe
                apply = abs(suggestion_t - t) > 1e-4
                if voiced and dbfs_at(samples, suggestion_t) > threshold and apply is False:
                    unresolvable = True
                if voiced and dbfs_at(samples, suggestion_t) > threshold and apply and min_db > threshold:
                    # Snapped but still voiced: still list as listen-here if no quieter dip.
                    unresolvable = True
                    apply = False
                    blocked = "no_gap"
        if unresolvable and voiced:
            listen_item = {
                "at_seconds": round(t, 3),
                "role": point.get("role") or "cut",
                "segment_index": point.get("segment_index"),
                "reason": blocked or "voiced",
                "listen_here": _listen_here(t, point.get("role"), blocked or "voiced"),
            }
            listen_here.append(listen_item)
        seam = {
            "index": index,
            "at_seconds": round(t, 3),
            "role": point.get("role") or "cut",
            "segment_index": point.get("segment_index"),
            "path": point.get("path") or point.get("source") or point.get("media_path"),
            "dbfs": round(db, 3),
            "local_min_seconds": round(min_t, 3),
            "local_min_dbfs": round(min_db, 3),
            "voiced": bool(voiced),
            "mid_word": bool(mid_word),
            "mid_word_text": (mid_word_hit or {}).get("word") or (mid_word_hit or {}).get("text") if mid_word_hit else None,
            "fade_seconds": round(fade, 4),
            "suggestion": {
                "at_seconds": round(suggestion_t, 3),
                "shift_ms": round((suggestion_t - t) * 1000.0, 1),
                "reason": reason,
                "apply": bool(apply),
                "blocked": blocked,
            },
            "unresolvable": bool(unresolvable and voiced),
        }
        if seam["unresolvable"]:
            seam["listen_here"] = _listen_here(t, seam["role"], blocked or "voiced")
        seams.append(seam)
    voiced_count = sum(1 for s in seams if s["voiced"])
    failed = bool(voiced_count or any(s["mid_word"] for s in seams))
    markdown = format_seams_markdown(
        {
            "seams": seams,
            "listen_here": listen_here,
            "voiced_threshold_db": threshold,
        }
    )
    return {
        "id": "seams",
        "ok": True,
        "pass": not failed,
        "mode": str(mode).lower(),
        "window_ms": float(window_ms),
        "voiced_threshold_db": threshold,
        "fade_seconds": float(fade_seconds),
        "sample_count": len(samples),
        "seams": seams,
        "voiced_count": voiced_count,
        "unresolvable": listen_here,
        "listen_here": listen_here,
        "markdown": markdown,
        "value": voiced_count,
        "limit": 0,
        "detail": (
            f"{voiced_count} voiced seam(s)"
            if voiced_count
            else None
        ),
    }


def _apply_point_to_item(item: dict[str, Any], role: str, new_t: float) -> None:
    if role == "in":
        _set_num(item, _START_KEYS, new_t)
    elif role == "out":
        end_keys = _END_KEYS if any(k in item for k in _END_KEYS) else ("end_seconds",)
        _set_num(item, end_keys, new_t)
    start = _num(item, _START_KEYS)
    end = _segment_end(item)
    if start is not None and end is not None and any(k in item for k in _LENGTH_KEYS):
        _set_num(item, _LENGTH_KEYS, end - start)


def _apply_suggestions(cuts: Any, seams: list[dict[str, Any]]) -> Any:
    cloned = copy.deepcopy(cuts)
    if isinstance(cloned, list) and cloned and all(isinstance(x, (int, float)) for x in cloned):
        by_index = {s["index"]: s for s in seams if (s.get("suggestion") or {}).get("apply")}
        return [
            float((by_index[i]["suggestion"]["at_seconds"]) if i in by_index else t)
            for i, t in enumerate(cloned)
        ]
    segments = _segment_dicts(cloned)
    for seam in seams:
        sug = seam.get("suggestion") or {}
        if not sug.get("apply"):
            continue
        idx = seam.get("segment_index")
        if idx is None or not (0 <= int(idx) < len(segments)):
            continue
        _apply_point_to_item(segments[int(idx)], str(seam.get("role") or "cut"), float(sug["at_seconds"]))
    return cloned


def snap_cuts(
    cuts: Any,
    mic_audio: Any = None,
    envelope: Any = None,
    words: Any = None,
    *,
    window_ms: float = WINDOW_MS,
    voiced_threshold_db: float = VOICED_THRESHOLD_DB,
    fade_seconds: float = DEFAULT_FADE_SECONDS,
    max_shift_ms: float | None = None,
    keep_words: bool = True,
    max_total_seconds: float | None = None,
    hop_seconds: float = 0.01,
) -> dict[str, Any]:
    """Apply seam suggestions without dropping covered words or exceeding a length cap.

    If applying every safe snap would exceed ``max_total_seconds``, the original
    cuts are returned unchanged and the would-be result is reported only.
    """
    report = check_seams(
        cuts,
        mic_audio=mic_audio,
        envelope=envelope,
        words=words,
        window_ms=window_ms,
        voiced_threshold_db=voiced_threshold_db,
        fade_seconds=fade_seconds,
        keep_words=keep_words,
        max_shift_ms=max_shift_ms,
        mode="fail",
        hop_seconds=hop_seconds,
    )
    snapped = _apply_suggestions(cuts, list(report.get("seams") or []))
    original_total = total_kept_seconds(cuts)
    new_total = total_kept_seconds(snapped)
    applied = True
    blocked_reason = None
    if max_total_seconds is not None and new_total > float(max_total_seconds) + 1e-6:
        applied = False
        blocked_reason = "max_total_seconds"
        snapped_out = cuts
    else:
        snapped_out = snapped
    applied_count = sum(
        1 for s in (report.get("seams") or []) if (s.get("suggestion") or {}).get("apply")
    )
    markdown = report.get("markdown") or format_seams_markdown(report)
    if blocked_reason == "max_total_seconds":
        markdown += (
            f"\nSnapping not applied: {new_total:.3f}s would exceed "
            f"max_total_seconds={float(max_total_seconds):g}.\n"
        )
    return {
        "ok": True,
        "applied": applied,
        "blocked_reason": blocked_reason,
        "cuts": snapped_out,
        "snapped": snapped,
        "original_cuts": cuts,
        "shifts": [
            {
                "index": s.get("index"),
                "role": s.get("role"),
                "from_seconds": s.get("at_seconds"),
                "to_seconds": (s.get("suggestion") or {}).get("at_seconds"),
                "shift_ms": (s.get("suggestion") or {}).get("shift_ms"),
            }
            for s in (report.get("seams") or [])
            if (s.get("suggestion") or {}).get("apply")
        ],
        "applied_count": applied_count if applied else 0,
        "total_seconds": total_kept_seconds(snapped_out),
        "snapped_total_seconds": new_total,
        "original_total_seconds": original_total,
        "max_total_seconds": max_total_seconds,
        "report": report,
        "seams": report.get("seams"),
        "listen_here": report.get("listen_here") or [],
        "markdown": markdown,
    }


def lf_352_boundary_spec() -> dict[str, Any]:
    """Public stand-in for the 352-boundary long-form spec in issue #37.

    Three known voiced seams (source seconds): s1 in 5795.30, s1 out
    6054.18, s4 out 4592.48. The other 349 boundaries sit in quiet gaps.
    """
    known = (
        ("s1", "in", 5795.30),
        ("s1", "out", 6054.18),
        ("s4", "out", 4592.48),
    )
    segments: list[dict[str, Any]] = [
        {"id": "s1", "start_seconds": 5795.30, "end_seconds": 6054.18, "fade_seconds": 0.04},
        {"id": "s4", "start_seconds": 4500.00, "end_seconds": 4592.48, "fade_seconds": 0.04},
    ]
    # 174 additional quiet segments → 352 boundaries total.
    t = 10.0
    while len(segments) < 176:
        segments.append(
            {
                "id": f"q{len(segments):03d}",
                "start_seconds": round(t, 3),
                "end_seconds": round(t + 1.2, 3),
                "fade_seconds": 0.04,
            }
        )
        t += 3.0
    envelope = [{"at_seconds": 0.0, "dbfs": -50.0}, {"at_seconds": 7000.0, "dbfs": -50.0}]
    for _sid, _role, at in known:
        envelope.extend(
            [
                {"at_seconds": round(at - 0.12, 3), "dbfs": -50.0},
                {"at_seconds": round(at - 0.04, 3), "dbfs": -18.0},
                {"at_seconds": round(at, 3), "dbfs": -16.0},
                {"at_seconds": round(at + 0.04, 3), "dbfs": -18.0},
                {"at_seconds": round(at + 0.12, 3), "dbfs": -50.0},
            ]
        )
    envelope.sort(key=lambda s: s["at_seconds"])
    words = [
        {"start_seconds": 5795.20, "end_seconds": 5795.55, "word": "still"},
        {"start_seconds": 6053.90, "end_seconds": 6054.30, "word": "voiced"},
        {"start_seconds": 4592.20, "end_seconds": 4592.60, "word": "seam"},
    ]
    return {
        "id": "lf_352",
        "segments": segments,
        "cuts": segments,
        "envelope": envelope,
        "words": words,
        "known_voiced": [{"id": sid, "role": role, "at_seconds": at} for sid, role, at in known],
        "boundary_count": 352,
    }
