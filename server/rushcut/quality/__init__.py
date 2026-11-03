"""Public, deterministic edit-quality checks. No clip-selection logic."""
from .check import format_report, run_checks
from .coverage import check_must_include
from .features import check_required_features, collect_applied_features
from .loop import check_loop
from .media import check_media, output_join_times
from .ramps import check_speed_ramps, extract_ramp_steps
from .seams import check_seams, format_seams_markdown, lf_352_boundary_spec, snap_cuts
from .sources import check_source_preference, classify_source
from .speech import (
    check_speech_boundaries,
    check_turn_completeness,
    coverage_ranges_from_cuts,
    cut_points_from_cuts,
)

__all__ = [
    "check_loop",
    "check_media",
    "check_must_include",
    "check_required_features",
    "check_seams",
    "check_source_preference",
    "check_speech_boundaries",
    "check_speed_ramps",
    "check_turn_completeness",
    "classify_source",
    "collect_applied_features",
    "coverage_ranges_from_cuts",
    "cut_points_from_cuts",
    "extract_ramp_steps",
    "format_report",
    "format_seams_markdown",
    "lf_352_boundary_spec",
    "output_join_times",
    "run_checks",
    "snap_cuts",
]
