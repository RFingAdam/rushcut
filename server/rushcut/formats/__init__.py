"""Story formats: a game-agnostic library of long-form gameplay structures.

``library.json`` holds the formats (titles, thumbnail, 0-15 s hook, spine,
section-end rule, narration pace, chapters, length, timing, pitfalls).
``plan_story_edit`` turns timestamped beats that the caller logged from the
footage into a beat sheet for one format. Nothing here reads media or ranks
footage: the caller supplies the beats.
"""
from .library import format_ids, get_story_format, list_story_formats, load_library
from .planner import (
    chapters_from_beats,
    format_timestamp,
    parse_value,
    plan_story_edit,
    validate_chapters,
)

__all__ = [
    "chapters_from_beats",
    "format_ids",
    "format_timestamp",
    "get_story_format",
    "list_story_formats",
    "load_library",
    "parse_value",
    "plan_story_edit",
    "validate_chapters",
]
