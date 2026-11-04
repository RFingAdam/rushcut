"""Run public quality checks: speech boundaries, features, source preference."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..analysis.transcript import analyze_transcript, speech_spans_from_audio
from ..profile import EditProfile, load_profile, profile_from_data
from ..render.sidecar import verify_output
from .coverage import check_must_include
from .delivery import check_duration_cap, check_frame1_not_black
from .features import check_required_features
from .loop import check_loop, check_loop_audio, check_loop_video
from .media import check_media
from .ramps import check_speed_ramps, extract_ramp_steps
from .seams import check_seams
from .sources import check_source_preference, sources_from_edit
from .speech import (
    check_speech_boundaries,
    check_turn_completeness,
    cut_points_from_cuts,
)


def _load_json(path: str | os.PathLike[str]) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _maybe_load(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    text = str(value)
    path = Path(text)
    if path.is_file():
        if path.suffix.lower() in {".yaml", ".yml"}:
            from ..profile import load_profile

            return load_profile(path).model_dump(by_alias=True)
        return _load_json(path)
    return value


def _load_profile(profile: Any) -> EditProfile | None:
    if profile is None:
        return None
    if isinstance(profile, EditProfile):
        return profile
    if isinstance(profile, (str, os.PathLike)):
        path = Path(profile)
        if path.is_file():
            return load_profile(path)
        raise FileNotFoundError(f"profile not found: {path}")
    if isinstance(profile, dict):
        return profile_from_data(profile)
    raise ValueError(f"profile must be a path, mapping, or EditProfile, not {type(profile).__name__}")


def _discover_run_dir(target: str) -> dict[str, Any]:
    """If target is a run directory, pick edit.json / profile / outputs."""
    path = Path(target)
    found: dict[str, Any] = {"dir": str(path)}
    for name in (
        "edit.json",
        "edit.v1.json",
        "profile.resolved.yaml",
        "profile.yaml",
        "edit_profile.yaml",
        "transcript.json",
        "report.json",
    ):
        candidate = path / name
        if candidate.is_file():
            found[name] = str(candidate)
    out_dir = path / "out"
    if out_dir.is_dir():
        found["out"] = str(out_dir)
        media = sorted(
            p for p in out_dir.iterdir() if p.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm"}
        )
        if media:
            found["output"] = str(media[0])
    edits = sorted(path.glob("edit*.json"))
    if edits and "edit.json" not in found:
        found["edit.json"] = str(edits[0])
    return found


def _edit_from_target(target: str | None, edit: Any) -> dict[str, Any] | None:
    if edit is not None:
        loaded = _maybe_load(edit)
        return loaded if isinstance(loaded, dict) else {"segments": loaded}
    if not target:
        return None
    path = Path(target)
    if path.is_file() and path.suffix.lower() == ".json":
        data = _load_json(path)
        return data if isinstance(data, dict) else {"segments": data}
    if path.is_dir():
        info = _discover_run_dir(str(path))
        edit_path = info.get("edit.json") or info.get("edit.v1.json")
        if edit_path:
            data = _load_json(edit_path)
            return data if isinstance(data, dict) else {"segments": data}
    return None


def _apply_gate(check: dict[str, Any], gate: str) -> dict[str, Any]:
    """Translate per-check pass into failure vs warning using the gate mode."""
    out = dict(check)
    mode = str(check.get("mode") or "fail").lower()
    skipped = bool(check.get("skipped"))
    passed = bool(check.get("pass"))
    out["severity"] = "info"
    if skipped or passed:
        return out
    if gate == "off" or mode == "off":
        out["pass"] = True
        out["severity"] = "info"
        out["note"] = (out.get("note") or "") + (" " if out.get("note") else "") + "gate=off"
        return out
    if gate == "warn" or mode == "warn":
        out["severity"] = "warning"
        # warn-mode checks do not fail the gate
        out["pass"] = True
        out["warned"] = True
        return out
    out["severity"] = "error"
    return out


def run_checks(
    *,
    target: str | None = None,
    profile: Any = None,
    transcript: Any = None,
    cuts: Any = None,
    edit: Any = None,
    applied_features: Any = None,
    sources: Any = None,
    available_sources: Any = None,
    pad_seconds: float | None = None,
    mode: str | None = None,
    reply_window_seconds: float | None = None,
    must_include: Any = None,
    loop: bool = False,
    check_media_output: bool = False,
    ssim_min: float | None = None,
    audio_jump_db: float | None = None,
    envelope: Any = None,
    mic_audio: Any = None,
    window_ms: float | None = None,
    voiced_threshold_db: float | None = None,
    ramp_pad_seconds: float | None = None,
    word_confidence_min: float | None = None,
    shift_ramps: bool = False,
) -> dict[str, Any]:
    """Run the public quality checks. Directors pass any cut list shape."""
    profile_obj: EditProfile | None = None
    profile_error: str | None = None
    if profile is not None:
        try:
            profile_obj = _load_profile(profile)
        except (OSError, ValueError) as exc:
            profile_error = str(exc)

    target_path = Path(target) if target else None
    run_info: dict[str, Any] | None = None
    if target_path and target_path.is_dir():
        run_info = _discover_run_dir(str(target_path))
        if profile_obj is None:
            for name in ("profile.resolved.yaml", "profile.yaml", "edit_profile.yaml"):
                if run_info.get(name):
                    try:
                        profile_obj = load_profile(run_info[name])
                    except (OSError, ValueError) as exc:
                        profile_error = str(exc)
                    break
        if transcript is None and run_info.get("transcript.json"):
            transcript = run_info["transcript.json"]

    edit_doc = _edit_from_target(target, edit)
    if cuts is None and edit_doc is not None:
        cuts = edit_doc
    cuts = _maybe_load(cuts)
    transcript_doc = _maybe_load(transcript)

    gate = str(mode or (profile_obj.quality.gate if profile_obj else "strict")).lower()
    if gate not in {"strict", "warn", "off"}:
        gate = "strict"

    pad = pad_seconds
    if pad is None and profile_obj is not None:
        pad = profile_obj.quality.speech_boundary.pad_seconds
    if pad is None:
        pad = 0.12

    speech_mode = profile_obj.quality.speech_boundary.mode if profile_obj else "fail"
    turn_mode = profile_obj.quality.turn_completeness.mode if profile_obj else "warn"
    seams_mode = profile_obj.quality.seams.mode if profile_obj else "warn"
    ramp_mode = profile_obj.quality.speed_ramps.mode if profile_obj else "fail"
    feature_mode = "fail"
    source_mode = profile_obj.source_preference.on_mismatch if profile_obj else "fail"
    if gate == "off":
        speech_mode = turn_mode = seams_mode = ramp_mode = feature_mode = source_mode = "off"

    words: list[dict[str, Any]] | None = None
    speech: Any = None
    if isinstance(transcript_doc, dict):
        words = list(transcript_doc.get("words") or [])
        speech = transcript_doc
    elif isinstance(transcript_doc, list):
        words = list(transcript_doc)
        speech = {"words": words}

    media_path: str | None = None
    if target_path and target_path.is_file() and target_path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        media_path = str(target_path)
    elif run_info and run_info.get("output"):
        media_path = str(run_info["output"])

    if speech is None and media_path and cut_points_from_cuts(cuts):
        # Prefer a real transcript; fall back to energy VAD so the check still runs.
        analyzed = analyze_transcript(media_path, diarize=False)
        if analyzed.get("ok") and analyzed.get("words"):
            speech = analyzed
            words = list(analyzed.get("words") or [])
        else:
            energy = speech_spans_from_audio(media_path)
            if energy.get("ok"):
                speech = {"speech_spans": energy.get("spans") or []}

    checks: list[dict[str, Any]] = []
    if profile_error:
        checks.append(
            {
                "id": "profile",
                "pass": False,
                "mode": "fail",
                "severity": "error",
                "detail": profile_error,
                "value": 1,
                "limit": 0,
            }
        )

    if cut_points_from_cuts(cuts) and speech is not None:
        raw = check_speech_boundaries(cuts, speech, pad_seconds=float(pad), mode=speech_mode)
        checks.append(_apply_gate(raw, gate))
        turn_words = words
        if turn_words is None and isinstance(speech, dict):
            turn_words = list(speech.get("words") or [])
        window = reply_window_seconds
        if window is None and profile_obj is not None:
            window = profile_obj.quality.turn_completeness.reply_window_seconds
        raw_turn = check_turn_completeness(
            cuts,
            turn_words,
            reply_window_seconds=float(window if window is not None else 2.5),
            mode=turn_mode,
        )
        checks.append(_apply_gate(raw_turn, gate))
    elif cut_points_from_cuts(cuts) and speech is None:
        checks.append(
            {
                "id": "speech_boundaries",
                "pass": True,
                "mode": speech_mode,
                "severity": "warning",
                "warned": True,
                "skipped": True,
                "note": "no transcript or speech spans; speech-boundary check skipped",
                "violations": [],
                "value": 0,
                "limit": 0,
            }
        )

    required = list(profile_obj.required_features) if profile_obj else []
    artifacts: dict[str, Any] = {}
    if run_info and run_info.get("out"):
        out_dir = Path(run_info["out"])
        for cap in out_dir.glob("*.srt"):
            artifacts["srt"] = str(cap)
            artifacts["srt_path"] = str(cap)
        for cap in out_dir.glob("*.ass"):
            artifacts["ass"] = str(cap)
            artifacts["ass_path"] = str(cap)
    if profile_obj is not None or applied_features is not None:
        raw_feat = check_required_features(
            required,
            applied=applied_features,
            edit=edit_doc,
            artifacts=artifacts,
            mode=feature_mode if required else "off",
        )
        checks.append(_apply_gate(raw_feat, gate))

    used_sources = sources
    if used_sources is None and edit_doc is not None:
        used_sources = sources_from_edit(edit_doc)
    avail = available_sources
    if avail is None and profile_obj is not None and profile_obj.sources:
        avail = [s.model_dump() for s in profile_obj.sources]
    if used_sources is not None or avail is not None:
        order = list(profile_obj.source_preference.order) if profile_obj else None
        raw_src = check_source_preference(
            used_sources,
            avail,
            order=order,
            on_mismatch=source_mode,
            mode=source_mode,
        )
        checks.append(_apply_gate(raw_src, gate))

    if must_include is not None:
        raw_must = check_must_include(cuts, _maybe_load(must_include), mode="fail")
        checks.append(_apply_gate(raw_must, gate))

    env = envelope
    mic = mic_audio
    if env is None and isinstance(edit_doc, dict):
        env = edit_doc.get("envelope")
    if mic is None and isinstance(edit_doc, dict):
        mic = edit_doc.get("mic_audio") or edit_doc.get("mic_path")
    if run_info:
        run_dir = Path(run_info["dir"])
        if env is None:
            for name in ("envelope.json", "mic_envelope.json"):
                candidate = run_dir / name
                if candidate.is_file():
                    env = str(candidate)
                    break
        if mic is None:
            for name in ("mic.wav", "mic.flac", "mic.m4a", "isolated_mic.wav"):
                candidate = run_dir / name
                if candidate.is_file():
                    mic = str(candidate)
                    break
    if isinstance(env, (str, os.PathLike)):
        env_path = Path(env)
        if env_path.is_file() and env_path.suffix.lower() in {".json", ".yaml", ".yml"}:
            env = _maybe_load(env)
    if (env is not None or mic is not None) and cut_points_from_cuts(cuts):
        seam_window = window_ms
        if seam_window is None and profile_obj is not None:
            seam_window = profile_obj.quality.seams.window_ms
        seam_threshold = voiced_threshold_db
        if seam_threshold is None and profile_obj is not None:
            seam_threshold = profile_obj.quality.seams.voiced_threshold_db
        seam_fade = profile_obj.quality.seams.fade_seconds if profile_obj else 0.04
        raw_seams = check_seams(
            cuts,
            mic_audio=mic,
            envelope=env,
            words=words,
            window_ms=float(seam_window if seam_window is not None else 300.0),
            voiced_threshold_db=float(seam_threshold if seam_threshold is not None else -32.0),
            fade_seconds=float(seam_fade),
            mode=seams_mode,
        )
        checks.append(_apply_gate(raw_seams, gate))

    ramp_spec = edit_doc if edit_doc is not None else cuts
    if extract_ramp_steps(ramp_spec):
        ramp_pad = ramp_pad_seconds
        if ramp_pad is None and profile_obj is not None:
            ramp_pad = profile_obj.quality.speed_ramps.pad_seconds
        conf = word_confidence_min
        if conf is None and profile_obj is not None:
            conf = profile_obj.quality.speed_ramps.confidence_min
        raw_ramps = check_speed_ramps(
            ramp_spec,
            words,
            pad_seconds=float(ramp_pad if ramp_pad is not None else 0.08),
            confidence_min=float(conf if conf is not None else 0.0),
            shift=bool(shift_ramps),
            mode=ramp_mode,
        )
        checks.append(_apply_gate(raw_ramps, gate))

    if media_path:
        verify = verify_output(media_path)
        verify_check = {
            "id": "verify_output",
            "pass": bool(verify.get("ok")),
            "mode": "fail",
            "value": 0 if verify.get("ok") else 1,
            "limit": 0,
            "problems": verify.get("problems") or [],
            "detail": "; ".join(verify.get("problems") or []) or None,
            "probe": verify.get("probe"),
        }
        if not verify.get("ok") and verify.get("error"):
            verify_check["detail"] = str(verify.get("error"))
        checks.append(_apply_gate(verify_check, gate))
        shorts = _shorts_settings(profile_obj)
        run_loop = bool(loop) or bool(shorts and shorts.loop)
        if shorts is not None:
            raw_cap = check_duration_cap(
                media_path,
                max_seconds=float(shorts.max_seconds),
                mode="fail",
            )
            checks.append(_apply_gate(raw_cap, gate))
        if run_loop:
            loop_ssim = ssim_min
            loop_jump = audio_jump_db
            loop_psnr = None
            loop_step = None
            if shorts is not None:
                if loop_ssim is None:
                    loop_ssim = shorts.ssim_min
                if loop_jump is None:
                    loop_jump = shorts.audio_jump_db
                loop_psnr = shorts.psnr_min
                loop_step = shorts.sample_step
            video_kwargs: dict[str, Any] = {"mode": "fail"}
            if loop_ssim is not None:
                video_kwargs["ssim_min"] = float(loop_ssim)
            if loop_psnr is not None:
                video_kwargs["psnr_min"] = float(loop_psnr)
            raw_video = check_loop_video(media_path, **video_kwargs)
            checks.append(_apply_gate(raw_video, gate))
            audio_kwargs: dict[str, Any] = {"mode": "fail"}
            if loop_jump is not None:
                audio_kwargs["audio_jump_db"] = float(loop_jump)
            if loop_step is not None:
                audio_kwargs["sample_step"] = float(loop_step)
            if shorts is not None:
                audio_kwargs["max_edge_fade_seconds"] = float(shorts.max_edge_fade_seconds)
            raw_audio = check_loop_audio(media_path, **audio_kwargs)
            checks.append(_apply_gate(raw_audio, gate))
            if shorts is not None:
                raw_black = check_frame1_not_black(
                    media_path,
                    black_luma=float(shorts.black_luma),
                    mode="fail",
                )
                checks.append(_apply_gate(raw_black, gate))
            if loop and shorts is None:
                loop_kwargs = {"mode": "fail"}
                if loop_ssim is not None:
                    loop_kwargs["ssim_min"] = float(loop_ssim)
                if loop_jump is not None:
                    loop_kwargs["audio_jump_db"] = float(loop_jump)
                raw_loop = check_loop(media_path, **loop_kwargs)
                checks.append(_apply_gate(raw_loop, gate))
        if check_media_output:
            media = check_media(
                media_path,
                cuts=cuts,
                transcript=speech,
                pad_seconds=float(pad),
                transcribe=False,
            )
            media_check = {
                "id": "check_media",
                "pass": bool(media.get("ok")) and not media.get("chopped_words"),
                "mode": "fail",
                "value": int(media.get("chopped_word_count") or 0),
                "limit": 0,
                "integrated_lufs": media.get("integrated_lufs"),
                "true_peak_dbtp": media.get("true_peak_dbtp"),
                "duration_seconds": media.get("duration_seconds"),
                "fps": media.get("fps"),
                "chopped_words": media.get("chopped_words") or [],
                "detail": (
                    f"LUFS {media.get('integrated_lufs')} peak {media.get('true_peak_dbtp')} "
                    f"{media.get('duration_seconds')}s"
                ),
            }
            if media.get("chopped_words"):
                media_check["detail"] = (
                    f"{media.get('chopped_word_count')} chopped word(s) at output joins"
                )
                media_check["pass"] = False
            checks.append(_apply_gate(media_check, gate))

    failures = [c for c in checks if not c.get("pass") and c.get("severity") == "error"]
    warnings = [c for c in checks if c.get("warned") or c.get("severity") == "warning"]
    passed = not failures
    ok = passed if gate != "off" else True
    result = {
        "ok": ok,
        "passed": passed,
        "gate": gate,
        "profile_id": profile_obj.id if profile_obj else None,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "target": os.path.abspath(target) if target else None,
    }
    report_dest = None
    if run_info and run_info.get("dir"):
        report_dest = Path(run_info["dir"]) / "report.json"
    elif media_path:
        report_dest = Path(media_path).with_name("report.json")
    if report_dest is not None:
        result["report_path"] = write_report(result, report_dest)
    return result


def _shorts_settings(profile_obj: Any) -> Any:
    if profile_obj is None:
        return None
    if getattr(profile_obj, "shorts", None) is not None:
        return profile_obj.shorts
    ident = (getattr(profile_obj, "id", None) or "").lower()
    if "shorts" in ident:
        from ..profile import ShortsSettings

        return ShortsSettings()
    return None


def write_report(result: dict[str, Any], dest: str | os.PathLike[str] | None = None) -> str | None:
    """Write report.json with ``{id, pass, value, limit}`` per check."""
    path: Path | None = None
    if dest:
        path = Path(dest)
        if path.is_dir() or str(dest).endswith(os.sep):
            path = path / "report.json"
    elif result.get("report_path"):
        path = Path(str(result["report_path"]))
    elif result.get("target"):
        target = Path(str(result["target"]))
        path = (target / "report.json") if target.is_dir() else target.with_name("report.json")
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = []
    for check in result.get("checks") or []:
        checks.append(
            {
                "id": check.get("id"),
                "pass": bool(check.get("pass")),
                "value": check.get("value"),
                "limit": check.get("limit"),
            }
        )
    payload = {
        "ok": bool(result.get("ok")),
        "passed": bool(result.get("passed")),
        "gate": result.get("gate"),
        "profile_id": result.get("profile_id"),
        "target": result.get("target"),
        "checks": checks,
        "failures": [c["id"] for c in checks if not c.get("pass")],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return str(path)


def format_report(result: dict[str, Any]) -> str:
    status = "PASS" if result.get("ok") else "FAIL"
    lines = [f"rushcut check: {status} (gate={result.get('gate')})"]
    for check in result.get("checks") or []:
        cid = check.get("id")
        if check.get("skipped") and check.get("pass"):
            mark = "skip"
        elif check.get("warned"):
            mark = "warn"
        elif check.get("pass"):
            mark = "pass"
        else:
            mark = "FAIL"
        detail = check.get("detail") or check.get("note") or ""
        n = check.get("value")
        extra = f" ({n})" if n and not check.get("pass") else ""
        line = f"  {mark} {cid}{extra}"
        if detail:
            line += f": {detail}"
        lines.append(line)
        for violation in (check.get("violations") or [])[:12]:
            at_s = violation.get("at_seconds")
            role = violation.get("role") or violation.get("speaker") or "cut"
            span = violation.get("span") or violation.get("dropped_turn") or {}
            text = span.get("text") or violation.get("detail") or ""
            span_t = ""
            if span.get("start_seconds") is not None:
                span_t = f" [{span.get('start_seconds'):.3f}-{span.get('end_seconds'):.3f}]"
            path = violation.get("path")
            loc = f"{at_s:.3f}s" if isinstance(at_s, (int, float)) else (path or "")
            lines.append(f"      {role:4} {loc}  {text}{span_t}".rstrip())
        for name in check.get("missing") or []:
            if isinstance(name, dict):
                ident = name.get("id") or name.get("t") or "event"
                detail = name.get("detail") or ""
                lines.append(f"      missing {ident} {detail}".rstrip())
            else:
                lines.append(f"      missing {name}")
        for item in (check.get("listen_here") or [])[:12]:
            if isinstance(item, dict):
                t = item.get("at_seconds")
                role = item.get("role") or "cut"
                why = item.get("reason") or "voiced"
                loc = f"{t:.3f}s" if isinstance(t, (int, float)) else ""
                lines.append(f"      listen here {loc}  {role}  {why}".rstrip())
            else:
                lines.append(f"      {item}")
        if check.get("id") == "seams" and check.get("markdown") and not check.get("pass"):
            # Keep the human table available on the text report.
            for table_line in str(check["markdown"]).splitlines()[2:8]:
                if table_line.startswith("|"):
                    lines.append(f"      {table_line}")
    return "\n".join(lines) + "\n"
