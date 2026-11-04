"""Prefer local recordings over platform VODs when both are available."""
from __future__ import annotations

import re
from typing import Any

KIND_LOCAL = "local_recording"
KIND_VOD = "platform_vod"
KIND_UNKNOWN = "unknown"

# Path hints only. Explicit `kind` on a source always wins.
_VOD_HINTS = (
    re.compile(r"twitch", re.IGNORECASE),
    re.compile(r"youtube", re.IGNORECASE),
    re.compile(r"youtu\.be", re.IGNORECASE),
    re.compile(r"yt-dlp", re.IGNORECASE),
    re.compile(r"kick\.com", re.IGNORECASE),
    re.compile(r"(^|[^a-z0-9])vods?([^a-z0-9]|$)", re.IGNORECASE),
    re.compile(r"platform.?vod", re.IGNORECASE),
    re.compile(r"streamable", re.IGNORECASE),
)
_LOCAL_HINTS = (
    re.compile(r"(^|[^a-z0-9])obs([^a-z0-9]|$)", re.IGNORECASE),
    re.compile(r"local.?record", re.IGNORECASE),
    re.compile(r"screen.?record", re.IGNORECASE),
    re.compile(r"game.?capture", re.IGNORECASE),
    re.compile(r"[\\/]recordings?[\\/]", re.IGNORECASE),
    re.compile(r"[\\/]captures?[\\/]", re.IGNORECASE),
)


def classify_source(path: str | None, kind: str | None = None) -> str:
    if kind:
        key = str(kind).strip().lower()
        if key in {KIND_LOCAL, "local", "recording", "local_record"}:
            return KIND_LOCAL
        if key in {KIND_VOD, "vod", "platform", "twitch", "youtube"}:
            return KIND_VOD
        return key
    text = str(path or "")
    vod = any(p.search(text) for p in _VOD_HINTS)
    local = any(p.search(text) for p in _LOCAL_HINTS)
    if vod and not local:
        return KIND_VOD
    if local and not vod:
        return KIND_LOCAL
    if vod and local:
        # e.g. recordings/twitch-backup.mkv — treat as local if it lives
        # under a capture folder, otherwise as a VOD.
        if any(p.search(text) for p in _LOCAL_HINTS[4:]):
            return KIND_LOCAL
        return KIND_VOD
    return KIND_UNKNOWN


def _normalize_sources(items: Any) -> list[dict[str, Any]]:
    if items is None:
        return []
    if isinstance(items, dict):
        if "path" in items or "source" in items:
            items = [items]
        else:
            items = list(items.values())
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            path = item
            kind = None
            if ":" in item and not re.match(r"^[A-Za-z]:[\\/]", item):
                path, kind = item.rsplit(":", 1)
            out.append(
                {
                    "path": path,
                    "kind": classify_source(path, kind),
                    "raw_kind": kind,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("source") or item.get("media_path") or ""
        kind = classify_source(str(path), item.get("kind") or item.get("source_kind"))
        out.append({"path": str(path), "kind": kind, "raw_kind": item.get("kind")})
    return out


def sources_from_edit(edit: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not edit:
        return []
    found: list[Any] = []
    if edit.get("sources"):
        found.extend(edit["sources"])
    for key in ("segments", "events", "clips"):
        for seg in edit.get(key) or []:
            if isinstance(seg, dict):
                found.append(seg)
    return _normalize_sources(found)


def check_source_preference(
    used: Any,
    available: Any = None,
    *,
    order: list[str] | None = None,
    on_mismatch: str = "fail",
    mode: str | None = None,
) -> dict[str, Any]:
    """Fail when a platform VOD was used while a local recording was available."""
    gate = str(mode or on_mismatch or "fail").lower()
    if gate == "off":
        return {
            "id": "source_preference",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "used": [],
            "available": [],
            "violations": [],
            "value": 0,
            "limit": 0,
        }
    ranking = list(order or [KIND_LOCAL, KIND_VOD])
    used_n = _normalize_sources(used)
    avail_n = _normalize_sources(available) if available is not None else list(used_n)
    used_kinds = {u["kind"] for u in used_n}
    avail_kinds = {a["kind"] for a in avail_n}

    def _rank(kind: str) -> int:
        try:
            return ranking.index(kind)
        except ValueError:
            return len(ranking)

    preferred = ranking[0] if ranking else KIND_LOCAL
    violations: list[dict[str, Any]] = []
    # If a better-ranked kind exists among available sources, using a
    # worse-ranked kind is a mismatch.
    best_available = min((_rank(k) for k in avail_kinds), default=None)
    if best_available is not None:
        for item in used_n:
            if _rank(item["kind"]) > best_available:
                violations.append(
                    {
                        "path": item.get("path"),
                        "kind": item["kind"],
                        "preferred": ranking[best_available] if best_available < len(ranking) else preferred,
                        "detail": (
                            f"used {item['kind']} while "
                            f"{ranking[best_available] if best_available < len(ranking) else preferred} "
                            "was available"
                        ),
                    }
                )
    # Special case of the motivation: platform VOD used, local recording present.
    if KIND_VOD in used_kinds and KIND_LOCAL in avail_kinds and not any(
        v.get("kind") == KIND_VOD for v in violations
    ):
        for item in used_n:
            if item["kind"] == KIND_VOD:
                violations.append(
                    {
                        "path": item.get("path"),
                        "kind": KIND_VOD,
                        "preferred": KIND_LOCAL,
                        "detail": "used platform_vod while local_recording was available",
                    }
                )
    # Dedup by path+kind
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in violations:
        key = (str(item.get("path") or ""), str(item.get("kind") or ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    note = None
    if KIND_VOD in used_kinds and KIND_LOCAL not in avail_kinds and not unique:
        note = "only platform_vod sources were provided; nothing local to prefer"
    return {
        "id": "source_preference",
        "pass": not unique,
        "mode": gate,
        "order": ranking,
        "used": used_n,
        "available": avail_n,
        "violations": unique,
        "value": len(unique),
        "limit": 0,
        "note": note,
        "detail": unique[0]["detail"] if unique else note,
    }
