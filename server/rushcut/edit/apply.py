"""Render a declarative edit.json timeline on FFmpeg (or build it on VEGAS)."""
from __future__ import annotations

import os
import shutil
from typing import Any

from ..analysis.ffmpeg_util import encode_timeout_seconds, run, which_ffmpeg
from ..analysis.probe import probe_media
from ..output_policy import check_output_path, make_work_dir
from ..render.atomic import run_ffmpeg_atomic
from ..render.sidecar import verify_output, write_sidecar
from .filters import (
    atempo_chain,
    blur_filter_complex,
    extra_strip_overlay,
    layout_filter,
    normalize_steps,
    overlay_filter,
    speed_output_length,
    zoom_filter,
)
from .schema import EditError, load_edit


def segment_source_length(segment: dict[str, Any]) -> float:
    return max(0.0, float(segment["out"]) - float(segment["in"]))


def segment_output_length(segment: dict[str, Any]) -> float:
    length = segment_source_length(segment)
    for effect in segment.get("effects") or []:
        if effect.get("type") == "speed_ramp":
            length = speed_output_length(length, normalize_steps(effect, length))
    return length


def expected_duration(edit: dict[str, Any]) -> float:
    return sum(segment_output_length(seg) for seg in edit["segments"])


def _encode_cmd(
    inputs: list[str],
    dest: str,
    vf: str | None = None,
    af: str | None = None,
    filter_complex: str | None = None,
    maps: list[str] | None = None,
    extra: list[str] | None = None,
    duration: float | None = None,
    fps: float | None = None,
    lossless: bool = False,
) -> list[str]:
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y"]
    for src in inputs:
        cmd.extend(["-i", src])
    if filter_complex:
        cmd.extend(["-filter_complex", filter_complex])
    if maps:
        for item in maps:
            cmd.extend(["-map", item])
    if vf:
        cmd.extend(["-vf", vf])
    if af:
        cmd.extend(["-af", af])
    if duration is not None:
        cmd.extend(["-t", f"{duration:.6f}"])
    if lossless:
        cmd.extend(["-c:v", "ffv1", "-c:a", "pcm_s16le"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2"])
        if fps:
            # fps filter is the conform; -r only tags the output.
            pass
    if extra:
        cmd.extend(extra)
    cmd.append(dest)
    return cmd


def _run_encode(cmd: list[str], dest: str, duration: float, width: int, height: int, fps: float) -> dict[str, Any]:
    timeout = encode_timeout_seconds(duration, width=width, height=height, fps=fps, preset="ultrafast")
    try:
        run(cmd, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not os.path.isfile(dest):
        return {"ok": False, "error": f"ffmpeg produced no file: {dest}"}
    return {"ok": True, "output_path": dest}


def _audio_mode(segment: dict[str, Any]) -> tuple[str, float]:
    src = segment.get("audio_source") or segment["source"]
    offset = float(segment.get("audio_offset") or 0.0)
    if segment.get("audio_source"):
        return str(src), (offset if offset else float(segment["in"]))
    return str(segment["source"]), float(segment["in"]) + offset


def _render_speed_piece(
    path: str,
    audio_path: str,
    take: float,
    audio_take: float,
    length: float,
    factor: float,
    dest: str,
    width: int,
    height: int,
    fps: float,
    audio_mode: str,
    tmix_above: float,
    bed: str | None,
    fade_s: float,
) -> dict[str, Any]:
    out_len = length / max(factor, 1e-6)
    vf_parts: list[str] = []
    if abs(factor - 1.0) > 1e-4:
        vf_parts.append(f"setpts=PTS/{factor:.6f}")
        if factor > float(tmix_above):
            frames = max(2, int(round(factor)))
            vf_parts.append(f"tmix=frames={frames}:weights={' '.join('1' for _ in range(frames))}")
    vf_parts.append(f"fps={fps}")
    vf_parts.append("format=yuv420p")
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-ss",
        f"{take:.6f}",
        "-t",
        f"{length:.6f}",
        "-i",
        path,
    ]
    af_parts: list[str] = []
    maps = ["-map", "0:v"]
    if audio_mode == "mute":
        cmd.extend(["-f", "lavfi", "-t", f"{out_len:.6f}", "-i", "anullsrc=r=48000:cl=stereo"])
        maps.extend(["-map", "1:a"])
    elif audio_mode == "keep_original_length":
        bed_path = bed or audio_path
        cmd.extend(["-ss", f"{audio_take:.6f}", "-t", f"{out_len:.6f}", "-i", bed_path])
        maps.extend(["-map", "1:a"])
        if fade_s > 0:
            af_parts.append(f"afade=t=in:st=0:d={fade_s:.4f}")
            af_parts.append(f"afade=t=out:st={max(0.0, out_len - fade_s):.4f}:d={fade_s:.4f}")
    else:
        cmd.extend(["-ss", f"{audio_take:.6f}", "-t", f"{length:.6f}", "-i", audio_path])
        maps.extend(["-map", "1:a"])
        if abs(factor - 1.0) > 1e-4:
            af_parts.append(atempo_chain(factor))
        if fade_s > 0:
            af_parts.append(f"afade=t=in:st=0:d={fade_s:.4f}")
            af_parts.append(f"afade=t=out:st={max(0.0, out_len - fade_s):.4f}:d={fade_s:.4f}")
    cmd.extend(["-vf", ",".join(vf_parts)])
    if af_parts:
        cmd.extend(["-af", ",".join(af_parts)])
    cmd.extend(maps)
    cmd.extend(
        [
            "-t",
            f"{out_len:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            dest,
        ]
    )
    return _run_encode(cmd, dest, out_len, width, height, fps)


def _concat_files(paths: list[str], dest: str, width: int, height: int, fps: float) -> dict[str, Any]:
    from ..analysis.edit_ops import concat_media

    return concat_media(paths, dest)


def _apply_vf_chain(
    src: str,
    dest: str,
    vf: str,
    width: int,
    height: int,
    fps: float,
    duration: float,
) -> dict[str, Any]:
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        src,
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        dest,
    ]
    return _run_encode(cmd, dest, duration, width, height, fps)


def _apply_complex(
    src: str,
    dest: str,
    graph: str,
    vlabel: str,
    width: int,
    height: int,
    fps: float,
    duration: float,
    extra_inputs: list[str] | None = None,
) -> dict[str, Any]:
    cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", src]
    for extra in extra_inputs or []:
        cmd.extend(["-i", extra])
    cmd.extend(
        [
            "-filter_complex",
            graph,
            "-map",
            f"[{vlabel}]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            dest,
        ]
    )
    return _run_encode(cmd, dest, duration, width, height, fps)


def _is_black_inset(path: str, rect: tuple[int, int, int, int], t: float, threshold: float = 10.0) -> bool:
    from .mediaio import extract_frame, mean_luma

    grabbed = extract_frame(path, t)
    if not grabbed.get("ok"):
        return False
    return mean_luma(grabbed["rgb"], grabbed["width"], grabbed["height"], rect) < threshold


def _render_plain_segment(
    segment: dict[str, Any],
    dest: str,
    width: int,
    height: int,
    fps: float,
    fade_s: float,
) -> dict[str, Any]:
    src = segment["source"]
    take = float(segment["in"])
    length = segment_source_length(segment)
    audio_path, audio_take = _audio_mode(segment)
    vf = f"fps={fps},format=yuv420p"
    af_parts = []
    if fade_s > 0:
        af_parts.append(f"afade=t=in:st=0:d={fade_s:.4f}")
        af_parts.append(f"afade=t=out:st={max(0.0, length - fade_s):.4f}:d={fade_s:.4f}")
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-ss",
        f"{take:.6f}",
        "-t",
        f"{length:.6f}",
        "-i",
        src,
        "-ss",
        f"{audio_take:.6f}",
        "-t",
        f"{length:.6f}",
        "-i",
        audio_path,
        "-vf",
        vf,
    ]
    if af_parts:
        cmd.extend(["-af", ",".join(af_parts)])
    cmd.extend(
        [
            "-map",
            "0:v",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-t",
            f"{length:.6f}",
            dest,
        ]
    )
    return _run_encode(cmd, dest, length, width, height, fps)


def _apply_effects(
    src: str,
    segment: dict[str, Any],
    dest: str,
    work: str,
    width: int,
    height: int,
    fps: float,
    source_probe: dict[str, Any],
) -> dict[str, Any]:
    current = src
    src_w = int(source_probe.get("width") or width)
    src_h = int(source_probe.get("height") or height)
    duration = float(probe_media(current).get("duration_seconds") or segment_output_length(segment))
    for i, effect in enumerate(segment.get("effects") or []):
        kind = effect.get("type")
        nxt = os.path.join(work, f"fx{i}_{kind}.mp4")
        if kind == "speed_ramp":
            # Already applied in the first pass when present.
            continue
        if kind == "zoom":
            vf = zoom_filter(effect, src_w, src_h, width, height) + f",fps={fps},format=yuv420p"
            result = _apply_vf_chain(current, nxt, vf, width, height, fps, duration)
        elif kind == "compose_layout":
            inset = effect.get("inset") or {}
            drop_empty = bool(effect.get("drop_empty_inset") or inset.get("drop_if_empty") or inset.get("drop_black"))
            if drop_empty and effect.get("layout") == "split":
                from .filters import _rect

                rect = _rect(inset.get("rect") or inset, src_w, src_h)
                if _is_black_inset(segment["source"], rect, float(segment["in"]) + 0.05):
                    effect = dict(effect)
                    effect["layout"] = "blur_fill"
                    effect["dropped_inset"] = True
            graph = layout_filter(effect, src_w, src_h, width, height)
            if graph.startswith("split") or "[bg]" in graph:
                labeled = f"[0:v]{graph}[vout]"
                result = _apply_complex(current, nxt, labeled, "vout", width, height, fps, duration)
            else:
                result = _apply_vf_chain(current, nxt, graph + f",fps={fps},format=yuv420p", width, height, fps, duration)
            for si, strip in enumerate(effect.get("strips") or []):
                if not isinstance(strip, dict):
                    continue
                vf_s, tx, ty = extra_strip_overlay("0:v", strip, src_w, src_h, width, height)
                strip_tmp = os.path.join(work, f"strip{si}.mp4")
                graph = f"[0:v]split[base][roi];[roi]{vf_s}[s];[base][s]overlay={tx}:{ty}[vout]"
                result = _apply_complex(nxt if result.get("ok") else current, strip_tmp, graph, "vout", width, height, fps, duration)
                if result.get("ok"):
                    nxt = strip_tmp
        elif kind == "blur_regions":
            regions = list(effect.get("regions") or [effect])
            graph, label = blur_filter_complex(regions, width, height)
            result = _apply_complex(current, nxt, graph, label, width, height, fps, duration)
        elif kind == "overlay":
            spec = overlay_filter(effect, width, height)
            ov = spec["path"]
            if not ov or not (os.path.isfile(ov) or spec["sequence"]):
                return {"ok": False, "error": f"overlay.path not found: {ov}", "field": "effects.overlay.path"}
            fade = spec["fade"]
            start = spec["start"]
            end = spec["end"]
            enable = f"gte(t,{start:.6f})" if end is None else f"between(t,{start:.6f},{end:.6f})"
            opacity = spec["opacity"]
            ov_parts: list[str] = ["format=rgba"]
            scale = spec["scale"]
            if abs(scale - 1.0) > 1e-3:
                ov_parts.append(f"scale=iw*{scale:.4f}:ih*{scale:.4f}")
            if fade > 0:
                ov_parts.append(f"fade=t=in:st={start:.4f}:d={fade:.4f}:alpha=1")
                if end is not None:
                    ov_parts.append(f"fade=t=out:st={max(start, float(end) - fade):.4f}:d={fade:.4f}:alpha=1")
            if opacity < 0.999:
                ov_parts.append(f"colorchannelmixer=aa={opacity:.4f}")
            ov_f = ",".join(ov_parts)
            graph = f"[1:v]{ov_f}[ov];[0:v][ov]overlay={spec['x']}:{spec['y']}:enable='{enable}'[vout]"
            cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", current]
            if spec["sequence"]:
                cmd.extend(["-framerate", str(spec["fps"]), "-i", ov])
            else:
                cmd.extend(["-i", ov])
            cmd.extend(
                [
                    "-filter_complex",
                    graph,
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    nxt,
                ]
            )
            result = _run_encode(cmd, nxt, duration, width, height, fps)
        elif kind == "lens_zoom":
            from .lens import apply_lens_zoom

            result = apply_lens_zoom(
                current,
                nxt,
                track=effect.get("track") or effect.get("track_file"),
                scale=float(effect.get("scale") or 1.6),
                inset_ratio=float(effect.get("inset") or effect.get("inset_ratio") or 0.08),
                feather_px=float(effect.get("feather") or effect.get("feather_px") or 4),
                ease=float(effect.get("ease") or effect.get("ease_seconds") or 0.25),
                focus_track=effect.get("focus") or effect.get("focus_track"),
                unsharp=bool(effect.get("unsharp", True)),
            )
        else:
            return {"ok": False, "error": f"unknown effect {kind!r}", "field": "effects.type"}
        if not result.get("ok"):
            return result
        current = nxt
        duration = float(probe_media(current).get("duration_seconds") or duration)
        src_w, src_h = width, height
    if current != dest:
        shutil.copy2(current, dest)
    return {"ok": True, "output_path": dest}


def render_segment(
    segment: dict[str, Any],
    dest: str,
    work: str,
    width: int,
    height: int,
    fps: float,
    fade_s: float,
) -> dict[str, Any]:
    src = segment["source"]
    if not os.path.isfile(src):
        return {"ok": False, "error": f"source not found: {src}", "field": "segments.source"}
    probe = probe_media(src)
    if not probe.get("ok"):
        return probe
    speed = next((e for e in segment.get("effects") or [] if e.get("type") == "speed_ramp"), None)
    base = os.path.join(work, "base.mp4")
    if speed:
        steps = normalize_steps(speed, segment_source_length(segment))
        audio_mode = str(speed.get("audio") or speed.get("audio_mode") or "atempo")
        tmix_above = float(speed.get("tmix_above") or speed.get("tmix") or 2.0)
        bed = speed.get("bed")
        audio_path, audio_take0 = _audio_mode(segment)
        pieces = []
        for i, step in enumerate(steps):
            piece = os.path.join(work, f"spd{i}.mp4")
            take = float(segment["in"]) + step["start"]
            audio_take = audio_take0 + step["start"]
            length = step["end"] - step["start"]
            result = _render_speed_piece(
                src,
                audio_path,
                take,
                audio_take,
                length,
                step["factor"],
                piece,
                width,
                height,
                fps,
                audio_mode,
                tmix_above,
                bed,
                fade_s if i == 0 or i == len(steps) - 1 else 0.0,
            )
            if not result.get("ok"):
                return result
            pieces.append(piece)
        if len(pieces) == 1:
            shutil.copy2(pieces[0], base)
        else:
            cat = _concat_files(pieces, base, width, height, fps)
            if not cat.get("ok"):
                return cat
    else:
        result = _render_plain_segment(segment, base, width, height, fps, fade_s)
        if not result.get("ok"):
            return result
    others = [e for e in segment.get("effects") or [] if e.get("type") != "speed_ramp"]
    current = base
    if others:
        fx_work = os.path.join(work, "fx")
        os.makedirs(fx_work, exist_ok=True)
        fx_dest = os.path.join(fx_work, "done.mp4")
        applied = _apply_effects(base, {**segment, "effects": others}, fx_dest, fx_work, width, height, fps, probe)
        if not applied.get("ok"):
            return applied
        current = fx_dest
    fitted = probe_media(current)
    need_scale = int(fitted.get("width") or 0) != width or int(fitted.get("height") or 0) != height
    out_len = segment_output_length(segment)
    if True:  # always conform fps / even size / exact duration
        vf = []
        if need_scale:
            vf.append(
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            )
        vf.append(f"fps={fps}")
        vf.append("format=yuv420p")
        cmd = [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            current,
            "-vf",
            ",".join(vf),
            "-t",
            f"{out_len:.6f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            dest,
        ]
        return _run_encode(cmd, dest, out_len, width, height, fps)
    shutil.copy2(current, dest)
    return {"ok": True, "output_path": dest}


def apply_edit(
    edit_json: str | dict[str, Any],
    output: str | None = None,
    *,
    backend: str | None = None,
    write_sidecar_file: bool = True,
) -> dict[str, Any]:
    try:
        edit = load_edit(edit_json)
    except EditError as exc:
        return {"ok": False, "error": str(exc), "field": exc.field}
    dest = output or (edit.get("output") or {}).get("path")
    if not dest:
        return {"ok": False, "error": "output is required", "field": "output"}
    refused = check_output_path(dest)
    if refused:
        return refused
    out = edit["output"]
    width, height, fps = int(out["width"]), int(out["height"]), float(out["fps"])
    fade_s = max(0.0, float(edit.get("join_fade_ms") or 0.0) / 1000.0)
    expected = expected_duration(edit)
    mode = (backend or os.environ.get("DIRECTOR_EDITOR") or "ffmpeg").lower()
    if mode in {"vegas", "resolve"}:
        from .vegas_map import apply_edit_host

        host = apply_edit_host(edit, dest, backend=mode)
        host.setdefault("expected_duration", expected)
        return host
    work = make_work_dir("rushcut-timeline-")
    try:
        pieces: list[str] = []
        for i, segment in enumerate(edit["segments"]):
            seg_dir = os.path.join(work, f"seg{i}")
            os.makedirs(seg_dir, exist_ok=True)
            piece = os.path.join(seg_dir, "out.mp4")
            result = render_segment(segment, piece, seg_dir, width, height, fps, fade_s)
            if not result.get("ok"):
                return result
            pieces.append(piece)
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        if len(pieces) == 1:
            joined = pieces[0]
        else:
            joined = os.path.join(work, "joined.mp4")
            cat = _concat_files(pieces, joined, width, height, fps)
            if not cat.get("ok"):
                return cat
        current = joined
        if edit.get("effects"):
            fx_dir = os.path.join(work, "global")
            os.makedirs(fx_dir, exist_ok=True)
            gdest = os.path.join(fx_dir, "out.mp4")
            dummy_probe = {"width": width, "height": height}
            result = _apply_effects(
                current,
                {"source": current, "in": 0, "out": expected, "effects": edit["effects"]},
                gdest,
                fx_dir,
                width,
                height,
                fps,
                dummy_probe,
            )
            if not result.get("ok"):
                return result
            current = gdest
        atomic = run_ffmpeg_atomic(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                current,
                "-t",
                f"{expected:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
            ],
            dest,
            timeout=encode_timeout_seconds(expected, width=width, height=height, fps=fps, preset="ultrafast"),
            verify_kwargs={"expect_audio": True, "expect_duration": expected, "duration_tolerance": 1.0 / max(fps, 1.0) + 0.02},
        )
        if not atomic.get("ok"):
            # copy fallback: just copy the file and verify
            shutil.copy2(current, dest)
            atomic = {"ok": True, "output_path": dest, "verify": verify_output(dest, expect_duration=expected, duration_tolerance=1.0 / max(fps, 1.0))}
        probe = probe_media(dest)
        duration = float(probe.get("duration_seconds") or 0)
        sidecar = None
        if write_sidecar_file:
            sidecar = write_sidecar(
                dest,
                {
                    "length": duration,
                    "length_seconds": duration,
                    "template": out.get("preset") or "timeline",
                    "zoom_count": sum(
                        1
                        for s in edit["segments"]
                        for e in s.get("effects") or []
                        if e.get("type") == "zoom"
                    ),
                },
                extra={"verify": atomic.get("verify") or probe},
                timeline=edit,
            )
        frame_tol = 1.0 / max(fps, 1.0)
        ok = abs(duration - expected) <= frame_tol + 0.02
        return {
            "ok": bool(ok and (atomic.get("ok") if isinstance(atomic, dict) else True)),
            "output_path": os.path.abspath(dest),
            "duration_seconds": duration,
            "expected_duration": expected,
            "width": width,
            "height": height,
            "fps": fps,
            "join_fade_ms": edit.get("join_fade_ms"),
            "segments": len(edit["segments"]),
            "sidecar_path": None if not sidecar else sidecar.get("sidecar_path"),
            "sidecar": sidecar,
            "verify": atomic.get("verify") if isinstance(atomic, dict) else None,
            "backend": "ffmpeg",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def blur_media(
    path: str,
    regions: list[dict[str, Any]],
    output_path: str,
    *,
    lossless: bool = False,
) -> dict[str, Any]:
    """File-level region blur / pixelate / fill."""
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"file not found: {path}"}
    refused = check_output_path(output_path)
    if refused:
        return refused
    probe = probe_media(path)
    if not probe.get("ok"):
        return probe
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    graph, label = blur_filter_complex(list(regions or []), width, height)
    cmd = [
        which_ffmpeg("ffmpeg"),
        "-hide_banner",
        "-y",
        "-i",
        path,
        "-filter_complex",
        graph,
        "-map",
        f"[{label}]",
        "-map",
        "0:a?",
    ]
    if lossless:
        cmd.extend(["-pix_fmt", "gbrp", "-c:v", "ffv1", "-c:a", "pcm_s16le"])
    else:
        cmd.extend(["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy"])
    cmd.append(output_path)
    try:
        run(cmd, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not os.path.isfile(output_path):
        return {"ok": False, "error": f"ffmpeg produced no file: {output_path}"}
    return {
        "ok": True,
        "output_path": os.path.abspath(output_path),
        "regions": len(regions or []),
        "lossless": lossless,
    }
