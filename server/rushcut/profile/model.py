"""Minimal edit-profile fields used by quality checks.

The full profile schema is #17. This module only models the public
safeguards: required_features, source_preference, and speech-boundary pad.
Unknown keys are kept so a later full schema can land without breaking files.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _coerce_mode(value: Any) -> Any:
    """YAML 1.1 treats `off`/`on` as booleans; accept that as gate/check modes."""
    if value is False:
        return "off"
    if isinstance(value, str):
        return value.strip().lower()
    return value

_SECRET_KEY = re.compile(r"(_key|_token|_secret)$", re.IGNORECASE)

GateMode = Literal["strict", "warn", "off"]
CheckMode = Literal["fail", "warn", "off"]
MismatchMode = Literal["fail", "warn", "off"]


class SourceItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    path: str
    kind: str | None = None


class SourcePreference(BaseModel):
    """Prefer local recordings over platform VODs when both exist."""

    model_config = ConfigDict(extra="allow")
    order: list[str] = Field(default_factory=lambda: ["local_recording", "platform_vod"])
    on_mismatch: MismatchMode = "fail"

    @field_validator("on_mismatch", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        return _coerce_mode(value)


class SpeechBoundarySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    pad_seconds: float = 0.12
    mode: CheckMode = "fail"

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        return _coerce_mode(value)

    @field_validator("pad_seconds")
    @classmethod
    def _pad_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quality.speech_boundary.pad_seconds: must be >= 0")
        return value


class TurnCompletenessSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    reply_window_seconds: float = 2.5
    mode: CheckMode = "warn"

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        return _coerce_mode(value)

    @field_validator("reply_window_seconds")
    @classmethod
    def _window_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quality.turn_completeness.reply_window_seconds: must be >= 0")
        return value


class SeamsSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    window_ms: float = 300.0
    voiced_threshold_db: float = -32.0
    fade_seconds: float = 0.04
    mode: CheckMode = "warn"

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        return _coerce_mode(value)

    @field_validator("window_ms")
    @classmethod
    def _window_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quality.seams.window_ms: must be >= 0")
        return value


class SpeedRampSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    pad_seconds: float = 0.08
    confidence_min: float = 0.0
    mode: CheckMode = "fail"

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> Any:
        return _coerce_mode(value)

    @field_validator("pad_seconds")
    @classmethod
    def _pad_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("quality.speed_ramps.pad_seconds: must be >= 0")
        return value


class QualitySettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    gate: GateMode = "strict"
    speech_boundary: SpeechBoundarySettings = Field(default_factory=SpeechBoundarySettings)
    turn_completeness: TurnCompletenessSettings = Field(default_factory=TurnCompletenessSettings)
    seams: SeamsSettings = Field(default_factory=SeamsSettings)
    speed_ramps: SpeedRampSettings = Field(default_factory=SpeedRampSettings)

    @field_validator("gate", mode="before")
    @classmethod
    def _gate(cls, value: Any) -> Any:
        return _coerce_mode(value)


class EditProfile(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_name: str = Field(default="rushcut.profile/v1", alias="schema")
    id: str | None = None
    label: str | None = None
    required_features: list[str] = Field(default_factory=list)
    source_preference: SourcePreference = Field(default_factory=SourcePreference)
    sources: list[SourceItem] = Field(default_factory=list)
    quality: QualitySettings = Field(default_factory=QualitySettings)

    @field_validator("required_features")
    @classmethod
    def _features_non_empty(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            name = str(item).strip()
            if not name:
                raise ValueError("required_features: entries must be non-empty strings")
            if name not in cleaned:
                cleaned.append(name)
        return cleaned

    @model_validator(mode="after")
    def _no_secrets(self) -> EditProfile:
        _reject_secrets(self.model_dump(by_alias=True))
        return self


def _reject_secrets(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)):
                raise ValueError(
                    f"{here}: secrets are not allowed in edit_profile.yaml; "
                    "use environment variables"
                )
            _reject_secrets(value, here)
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            _reject_secrets(value, f"{path}[{i}]")
