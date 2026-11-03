"""Public, deterministic edit-quality checks. No clip-selection logic."""
from .check import format_report, run_checks, write_report
from .coverage import check_must_include
from .delivery import check_duration_cap, check_frame1_not_black, stream_max_duration
from .features import check_required_features, collect_applied_features
from .loop import check_loop, check_loop_audio, check_loop_video
from .media import check_media, output_join_times
from .sources import check_source_preference, classify_source
from .speech import (
    check_speech_boundaries,
    check_turn_completeness,
    coverage_ranges_from_cuts,
    cut_points_from_cuts,
)

__all__ = [
    "check_duration_cap",
    "check_frame1_not_black",
    "check_loop",
    "check_loop_audio",
    "check_loop_video",
    "check_media",
    "check_must_include",
    "check_required_features",
    "check_source_preference",
    "check_speech_boundaries",
    "check_turn_completeness",
    "classify_source",
    "collect_applied_features",
    "coverage_ranges_from_cuts",
    "cut_points_from_cuts",
    "format_report",
    "output_join_times",
    "run_checks",
    "stream_max_duration",
    "write_report",
]
