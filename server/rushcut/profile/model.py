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


class ReviewSettings(BaseModel):
    """Where post-render frame sheets land."""

    model_config = ConfigDict(extra="allow")
    frame_check_dir: str | None = None


class ShortsSettings(BaseModel):
    """YouTube Shorts delivery: hard duration cap and seamless-loop checks."""

    model_config = ConfigDict(extra="allow")
    max_seconds: float = 30.0
    loop: bool = True
    ssim_min: float = 0.85
    psnr_min: float = 20.0
    audio_jump_db: float = 6.0
    sample_step: float = 0.5
    black_luma: float = 16.0
    max_edge_fade_seconds: float = 0.08

    @field_validator("max_seconds")
    @classmethod
    def _max_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("shorts.max_seconds: must be > 0")
        return value


class AudioTrackRole(BaseModel):
    """Named role for one source audio stream (mic vs mix)."""

    model_config = ConfigDict(extra="allow")
    role: str
    stream_index: int | None = None
    audio_index: int | None = None
    title: str | None = None


class AudioSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    tracks: list[AudioTrackRole] = Field(default_factory=list)


class LexiconSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    initial_prompt: str | None = None
    hotwords: list[str] = Field(default_factory=list)


class TranscriptSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    chunk_seconds: float = 600.0
    overlap_seconds: float = 30.0
    drift_threshold_s: float = 0.25
    model: str = "auto"


RectValue = list[float] | dict[str, float]


def _check_rect(value: Any, where: str) -> Any:
    if isinstance(value, list):
        if len(value) != 4:
            raise ValueError(f"{where}: rect must be [x, y, w, h]")
    elif isinstance(value, dict):
        if not any(k in value for k in ("w", "width")) or not any(k in value for k in ("h", "height")):
            raise ValueError(f"{where}: rect must have w and h")
    return value


class ScreenRegion(BaseModel):
    """A named on-screen area for read_screen_regions (OCR)."""

    model_config = ConfigDict(extra="allow")
    name: str
    rect: RectValue
    whitelist: str | None = None
    numeric: bool = False
    min_confidence: float | None = None
    psm: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("screen.regions[].name: must be non-empty")
        return value.strip()

    @field_validator("rect")
    @classmethod
    def _rect(cls, value: Any) -> Any:
        return _check_rect(value, "screen.regions[].rect")

    @field_validator("min_confidence")
    @classmethod
    def _conf(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("screen.regions[].min_confidence: must be in [0, 1]")
        return value


class ScreenTemplate(BaseModel):
    """A fixed graphic (PNG) for match_template, optionally tied to a region."""

    model_config = ConfigDict(extra="allow")
    name: str
    path: str
    threshold: float | None = None
    region: str | RectValue | None = None

    @field_validator("name", "path")
    @classmethod
    def _non_empty(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError(f"screen.templates[].{info.field_name}: must be non-empty")
        return value.strip()

    @field_validator("threshold")
    @classmethod
    def _threshold(cls, value: float | None) -> float | None:
        if value is not None and not -1.0 <= value <= 1.0:
            raise ValueError("screen.templates[].threshold: must be in [-1, 1]")
        return value

    @field_validator("region")
    @classmethod
    def _region(cls, value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return _check_rect(value, "screen.templates[].region")
        return value


class ScreenSettings(BaseModel):
    """On-screen regions and templates for read_screen_regions / match_template."""

    model_config = ConfigDict(extra="allow")
    regions: list[ScreenRegion] = Field(default_factory=list)
    templates: list[ScreenTemplate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names(self) -> ScreenSettings:
        region_names = [r.name for r in self.regions]
        if len(set(region_names)) != len(region_names):
            raise ValueError("screen.regions: names must be unique")
        template_names = [t.name for t in self.templates]
        if len(set(template_names)) != len(template_names):
            raise ValueError("screen.templates: names must be unique")
        for tpl in self.templates:
            if isinstance(tpl.region, str) and tpl.region not in region_names:
                raise ValueError(
                    f"screen.templates.{tpl.name}.region: unknown region {tpl.region!r}"
                )
        return self


class EditProfile(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    schema_name: str = Field(default="rushcut.profile/v1", alias="schema")
    id: str | None = None
    label: str | None = None
    required_features: list[str] = Field(default_factory=list)
    source_preference: SourcePreference = Field(default_factory=SourcePreference)
    sources: list[SourceItem] = Field(default_factory=list)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    shorts: ShortsSettings | None = None
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    frame_check_dir: str | None = None
    output_roots: list[str] = Field(default_factory=list)
    work_roots: list[str] = Field(default_factory=list)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    lexicon: LexiconSettings = Field(default_factory=LexiconSettings)
    transcript: TranscriptSettings = Field(default_factory=TranscriptSettings)
    screen: ScreenSettings | None = None

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

    @field_validator("output_roots", "work_roots")
    @classmethod
    def _roots_non_empty(cls, value: list[str], info: Any) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            name = str(item).strip()
            if not name:
                raise ValueError(f"{info.field_name}: entries must be non-empty strings")
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
