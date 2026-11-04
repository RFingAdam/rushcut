"""Load and query ``library.json`` (shipped as package data)."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib import resources
from typing import Any

REQUIRED_FIELDS = (
    "id",
    "name",
    "when_to_use",
    "title_templates",
    "thumbnail_template",
    "hook_0_15s",
    "spine",
    "section_end_rule",
    "narration",
    "chapters",
    "length_minutes",
    "timing",
    "pitfalls",
)


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    text = resources.files("rushcut.formats").joinpath("library.json").read_text(encoding="utf-8")
    return json.loads(text)


def load_library() -> dict[str, Any]:
    """The whole library as a deep copy (safe to mutate)."""
    return copy.deepcopy(_load())


def format_ids() -> list[str]:
    return [fmt["id"] for fmt in _load().get("formats") or []]


def _key(format_id: str) -> str:
    return (format_id or "").strip().lower().replace("-", "_").replace(" ", "_")


def get_story_format(format_id: str) -> dict[str, Any] | None:
    """One format by id (``timed-money-run`` and ``Timed Money Run`` also match)."""
    key = _key(format_id)
    for fmt in _load().get("formats") or []:
        if fmt["id"] == key:
            return copy.deepcopy(fmt)
    return None


def list_story_formats(summary_only: bool = False) -> dict[str, Any]:
    """Every format, plus the shared recipe, the pacing gate and the slot names."""
    lib = _load()
    formats = lib.get("formats") or []
    if summary_only:
        items: list[dict[str, Any]] = [
            {
                "id": fmt["id"],
                "name": fmt["name"],
                "when_to_use": fmt["when_to_use"],
                "length_minutes": fmt["length_minutes"],
                "timing_window": (fmt.get("timing") or {}).get("window"),
                "wpm_target": (fmt.get("narration") or {}).get("wpm_target"),
            }
            for fmt in formats
        ]
    else:
        items = copy.deepcopy(formats)
    return {
        "ok": True,
        "count": len(items),
        "ids": [fmt["id"] for fmt in formats],
        "formats": items,
        "shared_recipe": copy.deepcopy(lib.get("shared_recipe") or []),
        "pacing_gate": copy.deepcopy(lib.get("pacing_gate") or {}),
        "slots": copy.deepcopy(lib.get("slots") or {}),
        "schema_version": lib.get("schema_version"),
    }
