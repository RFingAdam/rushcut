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
            "Rushcut CLI. Quality checks: `rushcut check` "
            "(layout parity: `rushcut check --parity APPROVED CANDIDATE`). "
            "Session/media tools: `stitch-session`, `detect-region-presence`, "
            "`check-media`, `check-loop`, `check-seams`, `snap-cuts`. Story: `list-story-formats`, "
            "`plan-story-edit`, `measure-pacing`, `fits-cap`. "
            "Review: `export-review-pack`. Render: `rushcut apply edit.json -o out.mp4`. "
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
    check.add_argument(
        "--parity",
        metavar="APPROVED",
        help="Approved cut to compare the candidate against (layout-parity gate)",
    )
    check.add_argument(
        "--edit-region",
        action="append",
        default=None,
        dest="edit_region",
        help="Mask x0,y0,x1,y1,t0,t1 (repeatable; pixels ignored only in that span)",
    )
    check.add_argument(
        "--region",
        action="append",
        default=None,
        dest="parity_region",
        help="Named compare rect name=x0,y0,x1,y1 (repeatable)",
    )
    check.add_argument(
        "--time-map",
        action="append",
        nargs="+",
        default=None,
        dest="time_map",
        help="Candidate-to-approved map cand_t0,cand_t1,offset (repeatable)",
    )
    check.add_argument(
        "--samples",
        type=int,
        default=None,
        help="Parity sample count (default 24, minimum 20)",
    )
    check.add_argument("--ssim-min", type=float, default=None, dest="parity_ssim_min")
    check.add_argument("--mad-max", type=float, default=None)
    check.add_argument(
        "--presence-region",
        action="append",
        default=None,
        dest="presence_region",
        help="Fail if this named region has content on the approved frame and not the candidate",
    )
    check.add_argument(
        "--allow-new",
        type=float,
        default=None,
        dest="allow_new",
        help="Seconds of candidate-only tail (no_counterpart) to allow",
    )
    check.add_argument(
        "--seek-tolerance",
        type=int,
        default=None,
        dest="seek_tolerance",
        help="Search ±N approved frames for the best match at a trim seam",
    )
    check.add_argument(
        "--with-loop",
        action="store_true",
        help="With --parity, also run the loop-seam check on the candidate",
    )
    check.add_argument(
        "--with-loudness",
        action="store_true",
        help="With --parity, also run the loudness / check_media probe on the candidate",
    )
    check.add_argument("--envelope", help="Mic envelope JSON or audio path")
    check.add_argument("--mic-audio", dest="mic_audio", help="Isolated mic audio for seam scoring")
    check.add_argument(
        "--json",
        nargs="?",
        const=True,
        default=None,
        dest="json_out",
        metavar="PATH",
        help="Write JSON to PATH, or print JSON when PATH is omitted",
    )

    seams = sub.add_parser("check-seams", help="Score each cut against the mic envelope and word times")
    seams.add_argument("--cuts", required=True, help="JSON cut list or edit.json")
    seams.add_argument("--envelope", help="Envelope JSON ([{at_seconds, dbfs}])")
    seams.add_argument("--mic-audio", dest="mic_audio", help="Mic audio to build an envelope from")
    seams.add_argument("--transcript", help="transcript JSON (words)")
    seams.add_argument("--window-ms", type=float, default=300.0)
    seams.add_argument("--voiced-threshold-db", type=float, default=-32.0)
    seams.add_argument("--json", action="store_true", dest="as_json")

    snap = sub.add_parser("snap-cuts", help="Snap voiced seams to the nearest envelope dip")
    snap.add_argument("--cuts", required=True, help="JSON cut list or edit.json")
    snap.add_argument("--envelope", help="Envelope JSON")
    snap.add_argument("--mic-audio", dest="mic_audio")
    snap.add_argument("--transcript", help="transcript JSON (words)")
    snap.add_argument("--window-ms", type=float, default=300.0)
    snap.add_argument("--max-shift-ms", type=float, default=None)
    snap.add_argument("--max-total-seconds", type=float, default=None)
    snap.add_argument("--keep-words", action="store_true", default=True)
    snap.add_argument("--no-keep-words", dest="keep_words", action="store_false")
    snap.add_argument("--json", action="store_true", dest="as_json")

    loop = sub.add_parser("check-loop", help="SSIM last frames vs frame 1, plus audio-level continuity")
    loop.add_argument("file")
    loop.add_argument("--ssim-min", type=float, default=None)
    loop.add_argument("--audio-jump-db", type=float, default=None)
    loop.add_argument("--max-edge-fade", type=float, default=None, dest="max_edge_fade_seconds")
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

    repair = sub.add_parser(
        "repair-media",
        help="Stream-copy remux an unfinalized recording (never in place)",
    )
    repair.add_argument("path")
    repair.add_argument("--output", dest="output_path")
    repair.add_argument("--json", action="store_true", dest="as_json")

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

    apply_cmd = sub.add_parser(
        "apply",
        help="Render an edit.json timeline with FFmpeg (or build it on VEGAS / Resolve)",
    )
    apply_cmd.add_argument("edit", help="edit.json path or inline JSON object")
    apply_cmd.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output media path",
    )
    apply_cmd.add_argument(
        "--backend",
        default=None,
        help="ffmpeg (default), vegas, or resolve",
    )
    return parser


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _envelope_arg(value: str | None) -> Any:
    if value is None:
        return None
    from pathlib import Path

    path = Path(value)
    if path.is_file() and path.suffix.lower() in {".json", ".yaml", ".yml"}:
        return _load_json_arg(value)
    return value


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


def _emit_json(result: Any, json_out: Any) -> None:
    if not json_out:
        return
    if json_out is True:
        _print_json(result)
        return
    from pathlib import Path

    path = Path(str(json_out))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def _cmd_check_parity(args: argparse.Namespace) -> int:
    from .quality.parity import check_parity, format_parity_report

    if not args.target:
        print("rushcut check: error: candidate is required with --parity", file=sys.stderr)
        return 1
    kwargs: dict[str, Any] = {
        "approved": args.parity,
        "candidate": args.target,
        "edit_regions": args.edit_region,
        "regions": args.parity_region,
        "time_map": args.time_map,
        "presence_region": args.presence_region,
        "with_loop": bool(args.with_loop or args.loop),
        "with_loudness": bool(args.with_loudness or args.check_media_output),
    }
    if args.samples is not None:
        kwargs["samples"] = args.samples
    if args.parity_ssim_min is not None:
        kwargs["ssim_min"] = args.parity_ssim_min
    if args.mad_max is not None:
        kwargs["mad_max"] = args.mad_max
    if args.allow_new is not None:
        kwargs["allow_new"] = args.allow_new
    if args.seek_tolerance is not None:
        kwargs["seek_tolerance"] = args.seek_tolerance
    json_out = args.json_out
    if isinstance(json_out, str):
        kwargs["json_path"] = json_out
    try:
        result = check_parity(**kwargs)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"rushcut check: error: {exc}", file=sys.stderr)
        return 1
    if result.get("error") and not result.get("regions"):
        print(f"rushcut check: error: {result.get('error')}", file=sys.stderr)
        return 1
    if json_out is True:
        _print_json(result)
    else:
        sys.stdout.write(format_parity_report(result))
        if isinstance(json_out, str) and not result.get("json_path"):
            _emit_json(result, json_out)
    return 0 if result.get("ok") else 1


def _cmd_check(args: argparse.Namespace) -> int:
    if getattr(args, "parity", None):
        return _cmd_check_parity(args)
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
            envelope=_envelope_arg(args.envelope),
            mic_audio=args.mic_audio,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut check: error: {exc}", file=sys.stderr)
        return 1
    if args.json_out:
        if args.json_out is True:
            _print_json(result)
        else:
            _emit_json(result, args.json_out)
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
    if getattr(args, "max_edge_fade_seconds", None) is not None:
        kwargs["max_edge_fade_seconds"] = args.max_edge_fade_seconds
    result = check_loop(args.file, **kwargs)
    _print_json(result)
    if not result.get("ok") or not result.get("pass"):
        return 2
    return 0


def _cmd_check_seams(args: argparse.Namespace) -> int:
    from .quality.seams import check_seams

    try:
        result = check_seams(
            _load_json_arg(args.cuts),
            mic_audio=args.mic_audio,
            envelope=_envelope_arg(args.envelope),
            words=_load_json_arg(args.transcript),
            window_ms=args.window_ms,
            voiced_threshold_db=args.voiced_threshold_db,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut check-seams: error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        _print_json(result)
    else:
        sys.stdout.write(str(result.get("markdown") or "") or json.dumps(result, indent=2) + "\n")
    if not result.get("pass"):
        return 2
    return 0


def _cmd_snap_cuts(args: argparse.Namespace) -> int:
    from .quality.seams import snap_cuts

    try:
        result = snap_cuts(
            _load_json_arg(args.cuts),
            mic_audio=args.mic_audio,
            envelope=_envelope_arg(args.envelope),
            words=_load_json_arg(args.transcript),
            window_ms=args.window_ms,
            max_shift_ms=args.max_shift_ms,
            keep_words=bool(args.keep_words),
            max_total_seconds=args.max_total_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut snap-cuts: error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    if not result.get("ok"):
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


def _cmd_repair(args: argparse.Namespace) -> int:
    from .analysis.session import repair_media

    result = repair_media(args.path, args.output_path)
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


def _cmd_apply(args: argparse.Namespace) -> int:
    from .edit import apply_timeline

    try:
        payload: Any = _load_json_arg(args.edit)
        result = apply_timeline(edit_json=payload, output=args.output, backend=args.backend)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rushcut apply: error: {exc}", file=sys.stderr)
        return 1
    _print_json(result)
    if result.get("ok"):
        return 0
    err = result.get("error") or "apply failed"
    field = result.get("field")
    if field:
        print(f"rushcut apply: error: {field}: {err}", file=sys.stderr)
    else:
        print(f"rushcut apply: error: {err}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    commands = {
        "apply": _cmd_apply,
        "chapters-from-beats": _cmd_chapters,
        "check": _cmd_check,
        "check-loop": _cmd_check_loop,
        "check-media": _cmd_check_media,
        "check-seams": _cmd_check_seams,
        "detect-region-presence": _cmd_region,
        "export-review-pack": _cmd_export_review_pack,
        "fits-cap": _cmd_fits_cap,
        "list-story-formats": _cmd_list_formats,
        "measure-pacing": _cmd_pacing,
        "plan-story-edit": _cmd_plan,
        "profile": _cmd_profile,
        "repair-media": _cmd_repair,
        "snap-cuts": _cmd_snap_cuts,
        "stitch-session": _cmd_stitch,
    }
    handler = commands.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


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
    envelope: Any = None,
    mic_audio: Any = None,
    window_ms: float | None = None,
    voiced_threshold_db: float | None = None,
    ramp_pad_seconds: float | None = None,
    word_confidence_min: float | None = None,
    shift_ramps: bool = False,
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
            envelope=envelope,
            mic_audio=mic_audio,
            window_ms=window_ms,
            voiced_threshold_db=voiced_threshold_db,
            ramp_pad_seconds=ramp_pad_seconds,
            word_confidence_min=word_confidence_min,
            shift_ramps=shift_ramps,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
