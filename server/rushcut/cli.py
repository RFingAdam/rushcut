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
    check.add_argument("--json", action="store_true", dest="as_json", help="Print JSON report")

    profile = sub.add_parser("profile", help="Inspect an edit_profile.yaml")
    profile_sub = profile.add_subparsers(dest="profile_cmd")
    validate = profile_sub.add_parser("validate", help="Validate and print the profile")
    validate.add_argument("path")
    validate.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


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
        )
    except (OSError, ValueError) as exc:
        print(f"rushcut check: error: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        sys.stdout.write(format_report(result))
    if not result.get("ok"):
        return 2
    return 0


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
    if args.cmd == "profile":
        return _cmd_profile(args)
    parser.print_help()
    return 1


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
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
