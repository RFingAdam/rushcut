"""Load and validate edit_profile.yaml (quality-safeguard subset of #17)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .model import (
    CheckMode,
    EditProfile,
    GateMode,
    QualitySettings,
    SeamsSettings,
    SourceItem,
    SourcePreference,
    SpeechBoundarySettings,
    SpeedRampSettings,
    TurnCompletenessSettings,
)

__all__ = [
    "CheckMode",
    "EditProfile",
    "GateMode",
    "QualitySettings",
    "SeamsSettings",
    "SourceItem",
    "SourcePreference",
    "SpeechBoundarySettings",
    "SpeedRampSettings",
    "TurnCompletenessSettings",
    "load_profile",
    "profile_from_data",
    "validate_profile_file",
]


def profile_from_data(data: Any, *, source: str = "profile") -> EditProfile:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{source}: profile must be a mapping, not {type(data).__name__}")
    try:
        return EditProfile.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_format_validation(exc, source)) from exc


def _format_validation(exc: ValidationError, source: str) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc") or ())
        msg = err.get("msg") or "invalid"
        if loc:
            parts.append(f"{source}: {loc}: {msg}")
        else:
            parts.append(f"{source}: {msg}")
    return "; ".join(parts) if parts else f"{source}: invalid profile"


def load_profile(path: str | os.PathLike[str]) -> EditProfile:
    raw_path = Path(path)
    if not raw_path.is_file():
        raise FileNotFoundError(f"profile not found: {raw_path}")
    text = raw_path.read_text(encoding="utf-8")
    suffix = raw_path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{raw_path}: JSON parse error: {exc}") from exc
    else:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError(
                "PyYAML is required to load edit_profile.yaml. pip install pyyaml"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{raw_path}: YAML parse error: {exc}") from exc
    return profile_from_data(data, source=str(raw_path))


def validate_profile_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        profile = load_profile(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    payload = profile.model_dump(by_alias=True)
    from ..output_policy import policy_snapshot, report_stale_partials, set_active_profile

    set_active_profile(profile)
    snapshot = policy_snapshot(profile)
    return {
        "ok": True,
        "path": os.path.abspath(str(path)),
        "profile": payload,
        "id": profile.id,
        "required_features": list(profile.required_features),
        "source_preference": profile.source_preference.model_dump(),
        "output_roots": snapshot["output_roots"],
        "work_roots": snapshot["work_roots"],
        "output_policy": snapshot["output_policy"],
        "output_roots_source": snapshot["output_roots_source"],
        "stale_partials": report_stale_partials(),
    }
