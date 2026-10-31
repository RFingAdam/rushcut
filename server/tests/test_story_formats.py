from __future__ import annotations

import itertools
import json
from importlib import resources

import pytest

from rushcut.formats import (
    chapters_from_beats,
    format_timestamp,
    get_story_format,
    list_story_formats,
    load_library,
    parse_value,
    plan_story_edit,
    validate_chapters,
)
from rushcut.formats.library import REQUIRED_FIELDS

EXPECTED_IDS = {
    "timed_money_run",
    "unlock_grind",
    "price_ladder",
    "build_payoff",
    "narrated_solo_story",
    "season_series",
    "patch_week_take",
    "measured_test",
}

MONEY_BEATS = [
    {"t_sec": 0, "label": "Start", "value": "$0"},
    {"t_sec": 40, "label": "First contract"},
    {"t_sec": 3600, "label": "Hour 1", "value": "$84K"},
    {"t_sec": 3604, "label": "Close call"},
    {"t_sec": 7200, "label": "Hour 2", "value": "$190K"},
    {"t_sec": 10800, "label": "Hour 3", "value": "$1,032,798"},
    {"t_sec": 10830, "label": "Cash out"},
]


def _chapter_lengths(plan):
    times = [c["t_sec"] for c in plan["chapters"]] + [plan["planned_seconds"]]
    return [b - a for a, b in itertools.pairwise(times)]


def test_library_is_packaged_json_with_all_fields():
    raw = resources.files("rushcut.formats").joinpath("library.json").read_text(encoding="utf-8")
    lib = json.loads(raw)
    ids = [f["id"] for f in lib["formats"]]
    assert set(ids) == EXPECTED_IDS
    assert len(ids) == len(set(ids))
    for fmt in lib["formats"]:
        for field in REQUIRED_FIELDS:
            assert field in fmt, (fmt["id"], field)
        assert fmt["title_templates"]
        lo, hi = fmt["length_minutes"]
        assert 0 < lo < hi
        wlo, whi = fmt["narration"]["wpm_target"]
        assert 100 <= wlo < whi <= 240
        windows = [step["window_s"] for step in fmt["hook_0_15s"]]
        assert windows[0][0] == 0 and windows[-1][1] <= 15
        assert fmt["hook_0_15s"][0]["beat"] == "flash_forward"
        assert fmt["thumbnail_template"]["text_options"]
        assert fmt["pitfalls"]


def test_library_has_no_view_counts():
    text = json.dumps(load_library()).lower()
    assert "views" not in text


def test_list_story_formats_full_and_summary():
    full = list_story_formats()
    assert full["ok"] is True
    assert full["count"] == 8
    assert set(full["ids"]) == EXPECTED_IDS
    assert full["pacing_gate"]["dead_threshold_s"] == 8
    assert full["pacing_gate"]["chapters"]["min_count"] == 3
    summary = list_story_formats(summary_only=True)
    assert set(summary["formats"][0]) == {"id", "name", "when_to_use", "length_minutes", "timing_window", "wpm_target"}
    # Callers get copies, not the cached library.
    full["formats"][0]["name"] = "changed"
    assert list_story_formats()["formats"][0]["name"] != "changed"


def test_get_story_format_accepts_loose_ids():
    assert get_story_format("Timed-Money-Run")["id"] == "timed_money_run"
    assert get_story_format("nope") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (1032798, 1032798.0),
        ("$1,032,798", 1032798.0),
        ("48.2K", 48200.0),
        ("$1.2M", 1.2e6),
        ("HOUR 8", 8.0),
        ("3 of 10", 3.0),
        ("10 min", 10.0),
        ("M14", None),
        ("none", None),
        (True, None),
        (None, None),
    ],
)
def test_parse_value(value, expected):
    assert parse_value(value) == expected


def test_format_timestamp():
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(65.9) == "1:05"
    assert format_timestamp(3725) == "1:02:05"


def test_plan_money_run_beat_sheet():
    plan = plan_story_edit(
        "timed_money_run",
        MONEY_BEATS,
        12,
        slots={"game": "Test Game", "role": "Solo Sniper", "hours": 3, "goal": "a million in 3 hours"},
    )
    assert plan["ok"] is True
    # Strongest beat = largest value = the payoff.
    assert plan["cold_open"]["label"] == "Hour 3"
    ff = plan["cold_open"]["flash_forward"]
    assert 2.0 <= ff["source_end_s"] - ff["source_start_s"] <= 3.0
    assert ff["place_at_s"] == 0.0
    # The start value folds into section 1; every section ends on a value.
    assert [s["ends_on"]["label"] for s in plan["sections"]] == ["Hour 1", "Hour 2", "Hour 3"]
    assert plan["sections"][0]["beats"][0]["label"] == "Start"
    assert [b["label"] for b in plan["ending"]["beats"]] == ["Cash out"]
    # YouTube-valid chapters.
    assert plan["chapters"][0]["timestamp"] == "0:00"
    assert plan["chapters"][0]["title"] == "The goal: a million in 3 hours"
    assert len(plan["chapters"]) >= 3
    assert min(_chapter_lengths(plan)) >= 10
    assert plan["chapters_valid"] is True
    assert plan["chapters"][-1]["title"] == "Verdict"
    assert plan["chapters_text"].splitlines()[1].startswith("0:15 Hour 1: $84K")
    assert plan["planned_seconds"] == 720
    # VO slots only where beats are 8 s or more apart (not the 4 s "Close call").
    pairs = [(v["after"]["label"], v["before"]["label"]) for v in plan["vo_slots"]]
    assert ("Hour 1", "Close call") not in pairs
    assert ("Start", "First contract") in pairs
    assert ("Close call", "Hour 2") in pairs
    assert all(v["gap_s"] >= 8 for v in plan["vo_slots"])
    open_loop = next(v for v in plan["vo_slots"] if v["before"]["label"] == "Hour 2")
    assert open_loop["purpose"] == "open_loop"
    assert "$190K" not in open_loop["example_line"]  # never spoils the next number
    # Templates filled from beats and slots.
    titles = [t["text"] for t in plan["titles"]]
    assert "How Much Money Can a Solo Sniper Make in 3 Hours? (Test Game)" in titles
    assert "$0 to $1,032,798 in 3 Hours as a Solo Sniper | Test Game" in titles
    thumbs = [t["text"] for t in plan["thumbnails"]["text_options"]]
    assert "$0 → $1,032,798" in thumbs
    assert "HOUR 0 → HOUR 3" in thumbs
    assert "constraint" in plan["missing_slots"]
    assert plan["hook"][0]["text"] == "2-3 s of the final counter: $1,032,798."
    assert plan["narration"]["wpm_target"] == [145, 165]


def test_plan_prefers_marked_cold_open_then_strength():
    beats = [
        {"t_sec": 0, "label": "Cheap kit", "value": "$5K"},
        {"t_sec": 200, "label": "Mid kit", "value": "$20K", "strength": 0.9},
        {"t_sec": 400, "label": "Expensive kit", "value": "$50K"},
    ]
    assert plan_story_edit("price_ladder", beats, 8)["cold_open"]["label"] == "Mid kit"
    beats[0]["cold_open"] = True
    assert plan_story_edit("price_ladder", beats, 8)["cold_open"]["label"] == "Cheap kit"


def test_plan_merges_sections_to_fit_target():
    beats = [{"t_sec": i * 30, "label": f"Tip {i}", "value": i} for i in range(1, 41)]
    plan = plan_story_edit("patch_week_take", beats, 3)
    assert plan["ok"] is True
    # 3 min - 15 s hook - 20 s ending leaves room for 7 sections of 20 s.
    assert len(plan["sections"]) == 7
    assert any("Merged" in w for w in plan["warnings"])
    assert all(s["ends_on"] is not None for s in plan["sections"])
    assert plan["sections"][-1]["ends_on"]["label"] == "Tip 40"
    assert plan["chapters_valid"] is True
    assert min(_chapter_lengths(plan)) >= 10


def test_plan_short_footage_does_not_stretch():
    beats = [
        {"t_sec": 0, "label": "Supplies", "value": "12 crates"},
        {"t_sec": 60, "label": "Walls up", "value": "3 min"},
        {"t_sec": 120, "label": "First attack held", "value": "4 kills"},
    ]
    plan = plan_story_edit("build_payoff", beats, 10)
    assert plan["ok"] is True
    assert plan["planned_seconds"] < 600
    assert any("span" in w for w in plan["warnings"])
    assert plan["chapters_valid"] is True


def test_plan_without_values_warns():
    beats = [{"t_sec": 0, "label": "Spawn"}, {"t_sec": 300, "label": "Ambush"}, {"t_sec": 900, "label": "Escape"}]
    plan = plan_story_edit("narrated_solo_story", beats, 20)
    assert plan["ok"] is True
    assert any("No beat has a value" in w for w in plan["warnings"])
    assert plan["sections"][0]["ends_on"] is None
    assert plan["cold_open"]["label"] == "Escape"


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"format_id": "nope", "beats": MONEY_BEATS, "target_minutes": 10}, "unknown format_id"),
        ({"format_id": "unlock_grind", "beats": [], "target_minutes": 10}, "non-empty"),
        ({"format_id": "unlock_grind", "beats": [{"label": "x"}], "target_minutes": 10}, "t_sec"),
        ({"format_id": "unlock_grind", "beats": [{"t_sec": -1}], "target_minutes": 10}, ">= 0"),
        ({"format_id": "unlock_grind", "beats": MONEY_BEATS, "target_minutes": 0.5}, "at least"),
        ({"format_id": "unlock_grind", "beats": MONEY_BEATS, "target_minutes": 10, "vo_gap_seconds": 0}, "> 0"),
    ],
)
def test_plan_soft_fails(kwargs, needle):
    result = plan_story_edit(**kwargs)
    assert result["ok"] is False
    assert needle in result["error"]


def test_chapters_from_beats_rules():
    beats = [
        {"t_sec": 4, "label": "Cold open"},
        {"t_sec": 9, "label": "Too close"},
        {"t_sec": 45, "label": "Tier 1", "value": "LVL 5"},
        {"t_sec": 120, "label": "Tier 2", "value": "LVL 9"},
        {"t_sec": 595, "label": "Too near the end"},
    ]
    result = chapters_from_beats(beats, total_seconds=600)
    assert result["ok"] is True
    assert result["text"].splitlines() == ["0:00 Cold open", "0:45 Tier 1: LVL 5", "2:00 Tier 2: LVL 9"]
    reasons = {d["title"]: d["reason"] for d in result["dropped"]}
    assert "Too close" in reasons and "Too near the end" in reasons


def test_chapters_from_beats_adds_intro_and_fails_under_three():
    ok = chapters_from_beats([{"t_sec": 30, "label": "A"}, {"t_sec": 90, "label": "B"}])
    assert [c["title"] for c in ok["chapters"]] == ["Intro", "A", "B"]
    bad = chapters_from_beats([{"t_sec": 0, "label": "Only"}, {"t_sec": 5, "label": "Close"}])
    assert bad["ok"] is False
    assert "at least 3" in bad["error"]


def test_validate_chapters_flags_problems():
    rows = [{"t_sec": 2, "title": "A"}, {"t_sec": 8, "title": "B"}]
    check = validate_chapters(rows, total_seconds=12)
    assert check["valid"] is False
    joined = " ".join(check["problems"])
    assert "0:00" in joined and "at least 3" in joined and "10 s" in joined


def test_story_tools_registered_on_server():
    import rushcut

    listed = rushcut.list_story_formats(summary_only=True)
    assert listed["ok"] is True
    plan = rushcut.plan_story_edit("unlock_grind", MONEY_BEATS, 12)
    assert plan["ok"] is True
    chapters = rushcut.chapters_from_beats([{"t_sec": 0, "label": "a"}, {"t_sec": 20, "label": "b"}, {"t_sec": 40, "label": "c"}])
    assert chapters["ok"] is True
