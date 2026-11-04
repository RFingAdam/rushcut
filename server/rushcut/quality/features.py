"""Verify required_features from an edit profile were actually applied."""
from __future__ import annotations

import os
from typing import Any

# Generic names a renderer or director may record. Unknown names still work
# if they appear in applied_features; they must not be silently skipped.
_CAPTION_KEYS = ("captions", "caption", "srt", "ass", "subtitle", "subtitles")
_LOUDNESS_KEYS = ("loudness_normalize", "normalize_audio", "loudnorm", "master_audio")
_CHAPTER_KEYS = ("chapters", "chapter_list")
_BLUR_KEYS = ("blur_regions", "blur_media")


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    out: list[str] = []
    for item in value:
        name = str(item).strip()
        if name:
            out.append(name)
    return out


def collect_applied_features(
    *,
    applied: Any = None,
    edit: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> set[str]:
    found: set[str] = set(_as_str_list(applied))
    payload = edit or {}
    for key in ("applied_features", "features", "required_features_applied"):
        found.update(_as_str_list(payload.get(key)))
    effects = payload.get("effects") or payload.get("applied_effects") or []
    if isinstance(effects, list):
        for effect in effects:
            if isinstance(effect, str):
                found.add(effect)
            elif isinstance(effect, dict):
                name = effect.get("type") or effect.get("name") or effect.get("id")
                if name:
                    found.add(str(name))
    artifacts = artifacts or {}
    if payload.get("captions") or artifacts.get("srt") or artifacts.get("ass"):
        found.update(_CAPTION_KEYS[:1])
        found.add("captions")
    for path_key in ("srt_path", "ass_path", "captions_path"):
        path = artifacts.get(path_key) or payload.get(path_key)
        if path and os.path.isfile(str(path)):
            found.add("captions")
    audio = payload.get("audio") if isinstance(payload.get("audio"), dict) else {}
    if audio.get("normalize") or payload.get("loudness_normalize"):
        found.add("loudness_normalize")
    if payload.get("chapters"):
        found.add("chapters")
    return {name.strip() for name in found if name and str(name).strip()}


def _aliases(name: str) -> set[str]:
    key = name.strip().lower()
    groups = (_CAPTION_KEYS, _LOUDNESS_KEYS, _CHAPTER_KEYS, _BLUR_KEYS)
    for group in groups:
        if key in group:
            return set(group)
    return {key, name}


def check_required_features(
    required: list[str] | None,
    *,
    applied: Any = None,
    edit: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    mode: str = "fail",
) -> dict[str, Any]:
    """Fail when a profile-required feature is missing from the edit/output."""
    if str(mode).lower() == "off":
        return {
            "id": "required_features",
            "pass": True,
            "mode": "off",
            "skipped": True,
            "missing": [],
            "applied": [],
            "required": list(required or []),
            "value": 0,
            "limit": 0,
        }
    needed = [str(x).strip() for x in (required or []) if str(x).strip()]
    if not needed:
        return {
            "id": "required_features",
            "pass": True,
            "mode": str(mode).lower(),
            "skipped": True,
            "note": "no required_features in the profile",
            "missing": [],
            "applied": sorted(collect_applied_features(applied=applied, edit=edit, artifacts=artifacts)),
            "required": [],
            "value": 0,
            "limit": 0,
        }
    present = collect_applied_features(applied=applied, edit=edit, artifacts=artifacts)
    present_l = {p.lower() for p in present}
    missing: list[str] = []
    for name in needed:
        aliases = {a.lower() for a in _aliases(name)}
        if present_l.isdisjoint(aliases):
            missing.append(name)
    return {
        "id": "required_features",
        "pass": not missing,
        "mode": str(mode).lower(),
        "required": needed,
        "applied": sorted(present),
        "missing": missing,
        "value": len(missing),
        "limit": 0,
        "detail": (
            "required feature(s) were not applied: " + ", ".join(missing)
            if missing
            else None
        ),
    }
