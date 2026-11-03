"""Console entry for `rushcut`. `rushcut-mcp` still starts the MCP server."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .profile import validate_profile_file
from .quality import format_report, run_checks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rushcut",
        description=(
            "Rushcut CLI. Quality checks: `rushcut check`. "
            "Session/media tools: `stitch-session`, `detect-region-presence`, "
            "`check-media`, `check-loop`. Story: `list-story-formats`, "
            "`plan-story-edit`, `measure-pacing`, `fits-cap`. "
            "Review: `export-review-pack`. "
            "The MCP server is `rushcut-mcp` (or python -m rushcut with no args)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    check = sub.add_parser(
        "check",
        help="Run public quality checks on a render, edit.json, or run directory",
    )
    check.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Media file, edit.json, or run directory",
    )
    check.add_argument("--profile", help="edit_profile.yaml (or JSON)")
    check.add_argument("--transcript", help="transcript JSON (words + optional speakers)")
    check.add_argument("--cuts", help="JSON cut list or edit.json")
    check.add_argument("--edit", help="edit.json (segments, applied_features, sources)")
    check.add_argument(
        "--applied",
        default=None,
        help="Comma-separated features that were applied (captions,loudness_normalize,...)",
    )
    check.add_argument(
        "--sources",
        default=None,
        help="Comma-separated used sources, optionally path:kind",
    )
    check.add_argument(
        "--available-sources",
        default=None,
        dest="available_sources",
        help="Comma-separated sources that could have been used",
    )
    check.add_argument("--pad", type=float, default=None, help="Speech-boundary pad in seconds")
    check.add_argument(
        "--mode",
        choices=["strict", "warn", "off"],
        default=None,
        help="Gate mode (overrides profile quality.gate)",
    )
    check.add_argument(
        "--must-include",
        dest="must_include",
        help="JSON list of required events [{t, pre, post, id}] or a JSON file",
    )
    check.add_argument(
        "--loop",
        action="store_true",
        help="Also run the loop-seam check (SSIM last vs first, audio wrap)",
    )
    check.add_argument(
        "--check-media",
        action="store_true",
        dest="check_media_output",
        help="Also run check_media (LUFS, duration, fps, chopped words on the render)",
    )
    check.add_argument("--json", action="store_true", dest="as_json", help="Print JSON report")

    loop = sub.add_parser("check-loop", help="SSIM last frames vs frame 1, plus audio-level continuity")
    loop.add_argument("file")
    loop.add_argument("--ssim-min", type=float, default=None)
    loop.add_argument("--audio-jump-db", type=float, default=None)
    loop.add_argument("--json", action="store_true", dest="as_json")

    media = sub.add_parser(
        "check-media",
        help="Integrated LUFS, true peak, duration, fps, and speech-boundary re-check",
    )
    media.add_argument("file")
    media.add_argument("--cuts", help="JSON cut list (source segments or output join times)")
    media.add_argument("--transcript", help="transcript JSON to skip Whisper")
    media.add_argument("--pad", type=float, default=0.12)
    media.add_argument("--json", action="store_true", dest="as_json")

    stitch = sub.add_parser(
        "stitch-session",
        help="Group recording files by gap, remux unfinalized MKVs, concat, return an offset map",
    )
    stitch.add_argument("paths", nargs="+")
    stitch.add_argument("--gap", type=float, default=600.0, dest="gap_threshold_s")
    stitch.add_argument("--output", dest="output_path")
    stitch.add_argument("--session-index", type=int, default=None)
    stitch.add_argument("--json", action="store_true", dest="as_json")

    region = sub.add_parser(
        "detect-region-presence",
        help="Per-second overlay or face presence inside a region box",
    )
    region.add_argument("path")
    region.add_argument("--box", required=True, help="x,y,w,h (pixels, or 0-1 fractions)")
    region.add_argument("--mode", choices=["auto", "face", "activity"], default="auto")
    region.add_argument("--fps", type=float, default=1.0, dest="sample_fps")
    region.add_argument("--start", type=float, default=None, dest="start_seconds")
    region.add_argument("--end", type=float, default=None, dest="end_seconds")
    region.add_argument("--json", action="store_true", dest="as_json")

    formats = sub.add_parser("list-story-formats", help="List shipped long-form story formats")
    formats.add_argument("--summary", action="store_true")
    formats.add_argument("--json", action="store_true", dest="as_json")

    plan = sub.add_parser("plan-story-edit", help="Beat sheet from logged footage beats")
    plan.add_argument("--format", dest="format_id", required=True)
    plan.add_argument("--beats", required=True, help="JSON file or inline JSON list")
    plan.add_argument("--minutes", type=float, required=True, dest="target_minutes")
    plan.add_argument("--slots", help="JSON object of template slots")
    plan.add_argument("--json", action="store_true", dest="as_json")

    chapters = sub.add_parser("chapters-from-beats", help="YouTube chapters from edited-timeline beats")
    chapters.add_argument("--beats", required=True)
    chapters.add_argument("--total", type=float, default=None, dest="total_seconds")
    chapters.add_argument("--json", action="store_true", dest="as_json")

    pacing = sub.add_parser("measure-pacing", help="WPM, dead stretches, first-window words, time to promise")
    pacing.add_argument("media_path")
    pacing.add_argument("--dead-threshold", type=float, default=8.0)
    pacing.add_argument("--keywords", default=None, help="Comma-separated promise keywords")
    pacing.add_argument("--model-size", default="base")
    pacing.add_argument("--json", action="store_true", dest="as_json")

    profile = sub.add_parser("profile", help="Inspect an edit_profile.yaml")
    profile_sub = profile.add_subparsers(dest="profile_cmd")
    validate = profile_sub.add_parser("validate", help="Validate and print the profile")
    validate.add_argument("path")
    validate.add_argument("--json", action="store_true", dest="as_json")

    pack = sub.add_parser(
        "export-review-pack",
        help="Write 720p proxies, contact sheets, and QA.md into a review directory",
    )
    pack.add_argument("outputs", nargs="+")
    pack.add_argument("--dest", required=True)
    pack.add_argument("--proxy", default="720p")
    pack.add_argument("--chunk-mb", type=float, default=None)
    pack.add_argument("--profile", default=None)
    pack.add_argument("--json", action="store_true", dest="as_json")

    cap = sub.add_parser("fits-cap", help="Predict output duration from a segment spec")
    cap.add_argument("spec", help="JSON file or inline JSON segment spec")
    cap.add_argument("--max", type=float, default=None, dest="max_seconds")
    cap.add_argument("--fps", type=float, default=None)
    cap.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_json_arg(value: str | None) -> Any:
    if value is None:
        return None
    from pathlib import Path

    path = Path(value)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    text = value.strip()
    if text[:1] in "[{":
        return json.loads(text)
    return value


def _print_json(result: Any) -> None:
    print(json.dumps(result, indent=2))


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        result = run_checks(
            target=args.target,
            profile=args.profile,
            transcript=args.transcript,
            cuts=args.cuts,
            edit=args.edit,
            applied_features=_split_csv(args.applied),
            sources=_split_csv(args.sources),
            available_sources=_split_csv(args.available_sources),
            pad_seconds=args.pad,
            mode=args.mode,
            must_include=_load_json_arg(args.must_include),
            loop=bool(args.loop),
            check_media_output=bool(args.check_media_output),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut check: error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        _print_json(result)
    else:
        sys.stdout.write(format_report(result))
    if not result.get("ok"):
        return 2
    return 0


def _cmd_check_loop(args: argparse.Namespace) -> int:
    from .quality.loop import check_loop

    kwargs: dict[str, Any] = {}
    if args.ssim_min is not None:
        kwargs["ssim_min"] = args.ssim_min
    if args.audio_jump_db is not None:
        kwargs["audio_jump_db"] = args.audio_jump_db
    result = check_loop(args.file, **kwargs)
    _print_json(result)
    if not result.get("ok") or not result.get("pass"):
        return 2
    return 0


def _cmd_check_media(args: argparse.Namespace) -> int:
    from .quality.media import check_media

    try:
        result = check_media(
            args.file,
            cuts=_load_json_arg(args.cuts),
            transcript=_load_json_arg(args.transcript),
            pad_seconds=args.pad,
            transcribe=args.transcript is None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut check-media: error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    if not result.get("ok"):
        return 2
    return 0


def _cmd_stitch(args: argparse.Namespace) -> int:
    from .analysis.session import stitch_session

    result = stitch_session(
        args.paths,
        gap_threshold_s=args.gap_threshold_s,
        output_path=args.output_path,
        session_index=args.session_index,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _parse_box(text: str) -> list[float]:
    parts = [p.strip() for p in text.replace("x", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("box must be x,y,w,h")
    return [float(p) for p in parts]


def _cmd_region(args: argparse.Namespace) -> int:
    from .analysis.region import detect_region_presence

    try:
        box = _parse_box(args.box)
    except ValueError as exc:
        print(f"rushcut detect-region-presence: error: {exc}", file=sys.stderr)
        return 1
    result = detect_region_presence(
        args.path,
        box,
        mode=args.mode,
        sample_fps=args.sample_fps,
        start_seconds=args.start_seconds,
        end_seconds=args.end_seconds,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_list_formats(args: argparse.Namespace) -> int:
    from .formats import list_story_formats

    result = list_story_formats(summary_only=bool(args.summary))
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_plan(args: argparse.Namespace) -> int:
    from .formats import plan_story_edit

    try:
        beats = _load_json_arg(args.beats)
        slots = _load_json_arg(args.slots) if args.slots else None
    except json.JSONDecodeError as exc:
        print(f"rushcut plan-story-edit: error: {exc}", file=sys.stderr)
        return 1
    result = plan_story_edit(args.format_id, beats, args.target_minutes, slots=slots)
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_chapters(args: argparse.Namespace) -> int:
    from .formats import chapters_from_beats

    try:
        beats = _load_json_arg(args.beats)
    except json.JSONDecodeError as exc:
        print(f"rushcut chapters-from-beats: error: {exc}", file=sys.stderr)
        return 1
    result = chapters_from_beats(beats, total_seconds=args.total_seconds)
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_pacing(args: argparse.Namespace) -> int:
    from .analysis.pacing import measure_pacing

    result = measure_pacing(
        args.media_path,
        dead_threshold_s=args.dead_threshold,
        keywords=_split_csv(args.keywords),
        model_size=args.model_size,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_profile(args: argparse.Namespace) -> int:
    if args.profile_cmd != "validate":
        print("usage: rushcut profile validate <file>", file=sys.stderr)
        return 1
    result = validate_profile_file(args.path)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd == "check":
        return _cmd_check(args)
    if args.cmd == "check-loop":
        return _cmd_check_loop(args)
    if args.cmd == "check-media":
        return _cmd_check_media(args)
    if args.cmd == "stitch-session":
        return _cmd_stitch(args)
    if args.cmd == "detect-region-presence":
        return _cmd_region(args)
    if args.cmd == "list-story-formats":
        return _cmd_list_formats(args)
    if args.cmd == "plan-story-edit":
        return _cmd_plan(args)
    if args.cmd == "chapters-from-beats":
        return _cmd_chapters(args)
    if args.cmd == "measure-pacing":
        return _cmd_pacing(args)
    if args.cmd == "profile":
        return _cmd_profile(args)
    if args.cmd == "export-review-pack":
        return _cmd_export_review_pack(args)
    if args.cmd == "fits-cap":
        return _cmd_fits_cap(args)
    parser.print_help()
    return 1


def _cmd_export_review_pack(args: argparse.Namespace) -> int:
    from .render.review_pack import export_review_pack

    result = export_review_pack(
        args.outputs,
        args.dest,
        proxy=args.proxy,
        chunk_mb=args.chunk_mb,
        profile=args.profile,
    )
    _print_json(result)
    return 0 if result.get("ok") else 2


def _cmd_fits_cap(args: argparse.Namespace) -> int:
    from .formats import fits_cap

    try:
        spec = _load_json_arg(args.spec)
    except json.JSONDecodeError as exc:
        print(f"rushcut fits-cap: error: {exc}", file=sys.stderr)
        return 1
    result = fits_cap(spec, max_seconds=args.max_seconds, fps=args.fps)
    _print_json(result)
    return 0 if result.get("ok") and result.get("fits") else 2


def check_output(
    target: str | None = None,
    profile: str | None = None,
    transcript: Any = None,
    cuts: Any = None,
    edit: Any = None,
    applied_features: list[str] | None = None,
    sources: list[Any] | None = None,
    available_sources: list[Any] | None = None,
    pad_seconds: float | None = None,
    mode: str | None = None,
    must_include: Any = None,
    loop: bool = False,
    check_media_output: bool = False,
) -> dict[str, Any]:
    """Library/MCP facade for `rushcut check`."""
    try:
        return run_checks(
            target=target,
            profile=profile,
            transcript=transcript,
            cuts=cuts,
            edit=edit,
            applied_features=applied_features,
            sources=sources,
            available_sources=available_sources,
            pad_seconds=pad_seconds,
            mode=mode,
            must_include=must_include,
            loop=loop,
            check_media_output=check_media_output,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
