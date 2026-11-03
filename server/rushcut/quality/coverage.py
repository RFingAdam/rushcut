"""Required-event coverage: must_include events have to sit on the cut timeline."""
from __future__ import annotations

from typing import Any

from .speech import coverage_ranges_from_cuts


def _event_t(item: dict[str, Any]) -> float | None:
    for key in ("t", "t_sec", "at_seconds", "time", "seconds"):
        if key in item and item[key] is not None:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                continue
    return None


def _window_covered(lo: float, hi: float, ranges: list[tuple[float, float]], eps: float = 1e-6) -> bool:
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo <= eps:
        return any(a - eps <= lo < b + eps for a, b in ranges)
    covered = 0.0
    t = lo
    for a, b in ranges:
        if b <= t:
            continue
        if a > t + eps:
            return False
        covered_to = min(b, hi)
        if covered_to > t:
            covered += covered_to - t
            t = covered_to
        if t >= hi - eps:
            return True
    return t >= hi - eps


def check_must_include(
    cuts: Any,
    events: Any,
    *,
    mode: str = "fail",
) -> dict[str, Any]:
    """Fail when a required event's [t-pre, t+post] window is not on the cut list."""
    if str(mode).lower() == "off":
        return {
            "id": "must_include",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "missing": [],
            "value": 0,
            "limit": 0,
        }
    if events is None:
        return {
            "id": "must_include",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no must_include events",
            "missing": [],
            "value": 0,
            "limit": 0,
        }
    if isinstance(events, dict):
        events = events.get("must_include") or events.get("events") or events.get("items") or []
    items = list(events or [])
    if not items:
        return {
            "id": "must_include",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no must_include events",
            "missing": [],
            "value": 0,
            "limit": 0,
        }
    ranges = coverage_ranges_from_cuts(cuts)
    missing: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        if isinstance(raw, (int, float)):
            raw = {"t": float(raw), "id": str(index)}
        if not isinstance(raw, dict):
            missing.append({"id": str(index), "detail": "event is not an object"})
            continue
        t = _event_t(raw)
        if t is None:
            missing.append({"id": raw.get("id") or str(index), "detail": "event needs t"})
            continue
        pre = float(raw.get("pre") or raw.get("pre_s") or 0.0)
        post = float(raw.get("post") or raw.get("post_s") or 0.0)
        lo, hi = t - max(0.0, pre), t + max(0.0, post)
        if _window_covered(lo, hi, ranges):
            continue
        missing.append(
            {
                "id": raw.get("id") or str(index),
                "t": t,
                "pre": pre,
                "post": post,
                "start_seconds": round(lo, 3),
                "end_seconds": round(hi, 3),
                "detail": "required event is not on the cut timeline",
            }
        )
    return {
        "id": "must_include",
        "pass": not missing,
        "mode": str(mode).lower(),
        "events": len(items),
        "missing": missing,
        "value": len(missing),
        "limit": 0,
        "at_s": missing[0].get("t") if missing else None,
        "detail": (
            "required event(s) missing from the cut timeline: "
            + ", ".join(str(m.get("id")) for m in missing)
            if missing
            else None
        ),
    }
