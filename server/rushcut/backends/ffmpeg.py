"""Headless FFmpeg backend — full edits without a GUI NLE.

Timeline state is the same in-memory model as the OTIO interchange backend.
``render.start`` actually encodes with ffmpeg: trim, concat, transitions,
reframe, audio mix/normalize, caption burn-in.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import uuid
from typing import Any

from ..analysis.captions import burn_captions
from ..analysis.ffmpeg_util import FfmpegError, run, which_ffmpeg
from ..analysis.probe import probe_media
from .otio import OtioBackend
from .protocol import capability_sheet, fail, ok, unsupported

_PLACE_METHODS = {"media.place", "event.add_video", "event.add_audio", "event.add_title"}


class FfmpegBackend:
    name = "ffmpeg"

    def __init__(self, **_kwargs: Any) -> None:
        self._store = OtioBackend()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.last_mode = "ffmpeg"

    def ensure(self, project_path: str | None = None) -> dict[str, Any]:
        try:
            ffmpeg = which_ffmpeg("ffmpeg")
            ffprobe = which_ffmpeg("ffprobe")
        except FfmpegError as exc:
            return fail(str(exc), editor=self.name, used_mode="none")
        opened = None
        if project_path:
            opened = self._store.call("project.open", {"path": project_path, "discard_unsaved": True})
            if not opened.get("ok"):
                opened = dict(opened)
                opened["editor"] = self.name
                return opened
        out = ok(
            editor=self.name,
            used_mode="ffmpeg",
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            note="headless ffmpeg; no GUI NLE",
        )
        if opened:
            out["opened"] = opened
        self.last_mode = "ffmpeg"
        return out

    def info(self) -> dict[str, Any]:
        try:
            ffmpeg = which_ffmpeg("ffmpeg")
        except FfmpegError:
            ffmpeg = None
        return ok(
            host="ffmpeg",
            mode="ffmpeg",
            editor=self.name,
            product="FFmpeg",
            studio=False,
            version=1,
            ffmpeg=ffmpeg,
        )

    def capabilities(self) -> dict[str, Any]:
        sheet = capability_sheet(
            self.name,
            live=False,
            notes=(
                "Headless ffmpeg. Timeline is in-memory; render.start encodes. "
                "project.save still writes .otio / .fcpxml / .mlt as interchange."
            ),
            transport_play=False,
            transport_seek=False,
            motion_keyframes="partial",
            fades=True,
            envelopes="partial",
            titles=True,
            gain=True,
            alpha=False,
            render=True,
            markers=True,
        )
        sheet["studio_required"] = False
        sheet["platforms"] = ["windows", "macos", "linux"]
        sheet["export_formats"] = ["otio", "fcpxml", "mlt", "mp4"]
        return sheet

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method == "ping":
            return ok(host="ffmpeg", mode="ffmpeg", editor=self.name, used_mode="ffmpeg")
        if method in {"host.info", "editor.info"}:
            return self.info()
        if method == "editor.capabilities":
            return self.capabilities()
        if method == "render.list_templates":
            return self._list_templates(params)
        if method == "render.start":
            return self._render_start(params)
        if method == "render.status":
            return self._render_status(params)
        if method.startswith("transport."):
            return unsupported(method, self.name, "headless ffmpeg has no transport")
        if method in _PLACE_METHODS:
            result = self._place(method, params)
        else:
            result = self._store.call(method, params)
        if isinstance(result, dict):
            result = dict(result)
            result["editor"] = self.name
            result.setdefault("used_mode", "ffmpeg")
        return result

    # ----- place (probe unknown lengths) ------------------------------
    def _place(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = self._store.call(method, params)
        if not result.get("ok"):
            return result
        length = result.get("length_seconds")
        path = params.get("path") or params.get("media_path") or result.get("media_path")
        if length in (None, 0, 0.0, -1) and path and not str(path).startswith("title:"):
            probe = probe_media(str(path))
            if probe.get("ok"):
                take = float(params.get("take_offset_seconds") or 0.0)
                dur = max(0.0, float(probe.get("duration_seconds") or 0) - take)
                if dur > 0:
                    trim = self._store.call(
                        "event.trim",
                        {
                            "track_index": result.get("track_index", params.get("track_index", 0)),
                            "event_index": result.get("event_index", 0),
                            "length_seconds": dur,
                        },
                    )
                    if trim.get("ok"):
                        result = dict(result)
                        result["length_seconds"] = dur
        return result

    # ----- render -----------------------------------------------------
    def _list_templates(self, p: dict[str, Any]) -> dict[str, Any]:
        templates = [
            {
                "renderer": "ffmpeg",
                "renderer_name": "ffmpeg",
                "template": "Vertical 1080x1920",
                "width": 1080,
                "height": 1920,
            },
            {
                "renderer": "ffmpeg",
                "renderer_name": "ffmpeg",
                "template": "Internet HD 1080p",
                "width": 1920,
                "height": 1080,
            },
            {
                "renderer": "ffmpeg",
                "renderer_name": "ffmpeg",
                "template": "4K UHD 3840x2160",
                "width": 3840,
                "height": 2160,
            },
        ]
        rf = (p.get("renderer_contains") or "").lower()
        tf = (p.get("template_contains") or "").lower()
        items = [
            t
            for t in templates
            if (not rf or rf in t["renderer"].lower() or rf in t["template"].lower())
            and (not tf or tf in t["template"].lower())
        ]
        limit = int(p.get("limit") or 0)
        offset = int(p.get("offset") or 0)
        page = items[offset:] if limit <= 0 else items[offset : offset + limit]
        truncated = bool(limit > 0 and offset + limit < len(items))
        return ok(
            templates=page,
            count=len(page),
            truncated=truncated,
            offset=offset,
            limit=limit,
        )

    def _render_start(self, p: dict[str, Any]) -> dict[str, Any]:
        output_path = p.get("output_path")
        if not output_path:
            return fail("output_path is required", editor=self.name)
        async_render = p.get("async", True)
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "state": "running",
                "percent": 0,
                "output_path": output_path,
                "ok": True,
            }
        if async_render:
            thread = threading.Thread(target=self._run_job, args=(job_id, p), daemon=True)
            thread.start()
            return ok(
                job_id=job_id,
                state="running",
                async_render=True,
                output_path=output_path,
                editor=self.name,
                used_mode="ffmpeg",
            )
        self._run_job(job_id, p)
        return dict(self._jobs[job_id])

    def _render_status(self, p: dict[str, Any]) -> dict[str, Any]:
        job_id = p.get("job_id")
        with self._lock:
            if job_id:
                job = self._jobs.get(job_id)
            elif self._jobs:
                job = next(reversed(self._jobs.values()))
            else:
                job = None
        if not job:
            return fail("no render job", editor=self.name)
        return dict(job)

    def _run_job(self, job_id: str, p: dict[str, Any]) -> None:
        try:
            result = self._encode(p)
        except Exception as exc:  # noqa: BLE001 — surface any encoder crash as a job failure
            result = fail(f"ffmpeg render failed: {exc}", editor=self.name)
        result = dict(result)
        result.setdefault("job_id", job_id)
        result.setdefault("output_path", p.get("output_path"))
        result.setdefault("editor", self.name)
        result.setdefault("used_mode", "ffmpeg")
        if result.get("ok"):
            result["state"] = "complete"
            result["percent"] = 100
        else:
            result["state"] = "failed"
            result.setdefault("error", "render failed")
        with self._lock:
            self._jobs[job_id] = result

    def _encode(self, p: dict[str, Any]) -> dict[str, Any]:
        output_path = p["output_path"]
        os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
        state = self._store.call("project.get_state", {})
        if not state.get("ok"):
            return state
        width = int(state.get("width") or 1920)
        height = int(state.get("height") or 1080)
        fps = float(state.get("fps") or 24.0)
        template_name = (p.get("template_name") or "").lower()
        if "1080x1920" in template_name or "vertical" in template_name or "9:16" in template_name:
            width, height = 1080, 1920
        elif "3840" in template_name or "4k" in template_name or "2160" in template_name:
            width, height = 3840, 2160
        elif "1920x1080" in template_name or "1080p" in template_name:
            width, height = 1920, 1080

        tracks = list(self._store.tracks)
        video_tracks = [tr for tr in tracks if tr.get("type") == "video" and not tr.get("mute")]
        audio_tracks = [tr for tr in tracks if tr.get("type") == "audio" and not tr.get("mute")]
        if not video_tracks and not audio_tracks:
            return fail("timeline has no tracks to render", editor=self.name)

        range_start = float(p["start_seconds"]) if p.get("start_seconds") is not None else 0.0
        range_length = p.get("length_seconds")
        tmp = tempfile.mkdtemp(prefix="nle-ffmpeg-")
        try:
            program, duration = self._compose_program(
                video_tracks,
                audio_tracks,
                width,
                height,
                fps,
                tmp,
                range_start=range_start,
                range_length=None if range_length in (None, -1) else float(range_length),
            )
            if not program:
                return fail("nothing to render (no events)", editor=self.name)
            caption = p.get("ass_path") or p.get("caption_path")
            if caption and os.path.isfile(str(caption)):
                burned = burn_captions(program, str(caption), output_path)
                if not burned.get("ok"):
                    return burned
            else:
                shutil.copyfile(program, output_path)
            return ok(
                output_path=os.path.abspath(output_path),
                width=width,
                height=height,
                duration_seconds=duration,
                editor=self.name,
            )
        except FfmpegError as exc:
            return fail(str(exc), editor=self.name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _compose_program(
        self,
        video_tracks: list[dict[str, Any]],
        audio_tracks: list[dict[str, Any]],
        width: int,
        height: int,
        fps: float,
        tmp: str,
        *,
        range_start: float,
        range_length: float | None,
    ) -> tuple[str | None, float]:
        base_events = list(video_tracks[0]["events"]) if video_tracks else []
        extra_video = video_tracks[1:] if len(video_tracks) > 1 else []
        if not base_events and extra_video:
            base_events = list(extra_video[0]["events"])
            extra_video = extra_video[1:]

        if not base_events and not audio_tracks:
            # Titles-only or empty. Synthesize black from title events on any track.
            titles = [ev for tr in video_tracks for ev in tr.get("events") or []]
            if not titles:
                return None, 0.0
            base_events = titles
            extra_video = []

        segments: list[tuple[str, float, float]] = []  # path, timeline_start, overlap_with_next
        ordered = sorted(base_events, key=lambda e: float(e.get("start_seconds") or 0))
        for i, ev in enumerate(ordered):
            start = float(ev.get("start_seconds") or 0)
            length = float(ev.get("length_seconds") or 0)
            if length <= 0:
                continue
            if range_length is not None:
                range_end = range_start + range_length
                if start + length <= range_start or start >= range_end:
                    continue
            seg = os.path.join(tmp, f"v{i}.mp4")
            self._encode_event(ev, seg, width, height, fps)
            overlap = 0.0
            if i + 1 < len(ordered):
                nxt = ordered[i + 1]
                nstart = float(nxt.get("start_seconds") or 0)
                overlap = max(0.0, start + length - nstart)
                dissolve = bool((ev.get("fades") or {}).get("dissolve")) or bool(
                    (nxt.get("fades") or {}).get("dissolve")
                )
                if not dissolve:
                    overlap = 0.0
            segments.append((seg, start, overlap))

        if not segments:
            # Audio-only: black video of mixed audio duration.
            audio_end = 0.0
            for tr in audio_tracks:
                for ev in tr.get("events") or []:
                    audio_end = max(
                        audio_end,
                        float(ev.get("start_seconds") or 0) + float(ev.get("length_seconds") or 0),
                    )
            if audio_end <= 0:
                return None, 0.0
            black = os.path.join(tmp, "black.mp4")
            self._black_segment(black, audio_end, width, height, fps)
            current = black
            duration = audio_end
        else:
            # Fill a leading gap with black so overlays/audio line up.
            lead = segments[0][1]
            if lead > 0.05:
                gap = os.path.join(tmp, "lead.mp4")
                self._black_segment(gap, lead, width, height, fps)
                segments.insert(0, (gap, 0.0, 0.0))
            current = segments[0][0]
            for i in range(1, len(segments)):
                nxt, _start, _ov = segments[i]
                overlap = segments[i - 1][2]
                joined = os.path.join(tmp, f"join{i}.mp4")
                if overlap > 0.05:
                    self._xfade(current, nxt, overlap, joined)
                else:
                    self._concat_two(current, nxt, joined)
                current = joined
            duration = float(probe_media(current).get("duration_seconds") or 0)

        # Overlay additional video tracks (titles, graphics).
        for ti, tr in enumerate(extra_video):
            for ei, ev in enumerate(tr.get("events") or []):
                overlay = os.path.join(tmp, f"ov{ti}_{ei}.mp4")
                self._encode_event(ev, overlay, width, height, fps)
                start = float(ev.get("start_seconds") or 0)
                length = float(ev.get("length_seconds") or 0)
                mixed = os.path.join(tmp, f"ovmix{ti}_{ei}.mp4")
                self._overlay(current, overlay, start, length, mixed)
                current = mixed

        # Mix dedicated audio tracks onto the program.
        extra_audio: list[tuple[str, float, dict[str, Any], dict[str, Any]]] = []
        for tr in audio_tracks:
            for ev in tr.get("events") or []:
                path = ev.get("media_path")
                if not path or not os.path.isfile(path):
                    continue
                extra_audio.append((path, float(ev.get("start_seconds") or 0), ev, tr))
        want_norm = any(
            ev.get("normalize")
            for tr in (video_tracks + audio_tracks)
            for ev in tr.get("events") or []
        )
        if extra_audio or want_norm:
            mixed = os.path.join(tmp, "amix.mp4")
            self._mix_audio_onto(current, extra_audio, mixed, normalize=want_norm)
            current = mixed

        return current, duration

    def _encode_event(
        self,
        ev: dict[str, Any],
        dest: str,
        width: int,
        height: int,
        fps: float,
    ) -> None:
        path = ev.get("media_path") or ""
        length = max(0.04, float(ev.get("length_seconds") or 0.04))
        take = float(ev.get("take_offset_seconds") or 0.0)
        fades = ev.get("fades") or {}
        fade_in = float(fades.get("fade_in_seconds") or 0) or 0.0
        fade_out = float(fades.get("fade_out_seconds") or 0) or 0.0
        motion = list(ev.get("motion") or [])
        scale = 1.0
        pan_x = 0.0
        pan_y = 0.0
        if motion:
            last = motion[-1]
            scale = float(last.get("scale") or 1.0)
            pan_x = float(last.get("pan_x") or 0.0)
            pan_y = float(last.get("pan_y") or 0.0)
        zoom = max(1.0, scale)
        vf_parts = [
            f"scale={width * zoom}:{height * zoom}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}:(iw-{width})/2*(1+{pan_x}):(ih-{height})/2*(1+{pan_y})",
            f"fps={fps}",
            "format=yuv420p",
        ]
        if fade_in > 0:
            vf_parts.append(f"fade=t=in:st=0:d={fade_in:.4f}")
        if fade_out > 0:
            start = max(0.0, length - fade_out)
            vf_parts.append(f"fade=t=out:st={start:.4f}:d={fade_out:.4f}")
        opacity = ev.get("opacity")
        if opacity is not None and float(opacity) < 0.999:
            vf_parts.append(f"colorchannelmixer=aa={float(opacity):.4f}")
        if str(path).startswith("title:") or ev.get("kind") == "title":
            title = (ev.get("name") or "Title").replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")
            vf_parts.append(
                f"drawtext=text='{title}':fontcolor=white:fontsize={max(24, height // 18)}:"
                f"x=(w-text_w)/2:y=h-h/8:borderw=2"
            )
            cmd = [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}:d={length:.6f}:r={fps}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=48000:cl=stereo:d={length:.6f}",
                "-vf",
                ",".join(vf_parts),
                "-shortest",
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
            run(cmd, timeout=120)
            return
        if not path or not os.path.isfile(path):
            self._black_segment(dest, length, width, height, fps)
            return
        probe = probe_media(path)
        cmd = [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-ss",
            f"{take:.6f}",
            "-i",
            path,
            "-t",
            f"{length:.6f}",
        ]
        af_parts: list[str] = []
        gain = float(ev.get("gain") or 1.0)
        if abs(gain - 1.0) > 1e-3:
            af_parts.append(f"volume={gain:.4f}")
        if fade_in > 0:
            af_parts.append(f"afade=t=in:st=0:d={fade_in:.4f}")
        if fade_out > 0:
            start = max(0.0, length - fade_out)
            af_parts.append(f"afade=t=out:st={start:.4f}:d={fade_out:.4f}")
        if probe.get("has_video"):
            cmd.extend(["-vf", ",".join(vf_parts)])
        else:
            cmd.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=black:s={width}x{height}:d={length:.6f}:r={fps}",
                    "-filter_complex",
                    "[1:v]format=yuv420p[v]",
                    "-map",
                    "[v]",
                    "-map",
                    "0:a?",
                ]
            )
        if probe.get("has_audio") and af_parts:
            cmd.extend(["-af", ",".join(af_parts)])
        elif not probe.get("has_audio"):
            cmd.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r=48000:cl=stereo:d={length:.6f}",
                    "-shortest",
                ]
            )
        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                dest,
            ]
        )
        run(cmd, timeout=180)

    def _black_segment(self, dest: str, length: float, width: int, height: int, fps: float) -> None:
        length = max(0.04, length)
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={width}x{height}:d={length:.6f}:r={fps}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=48000:cl=stereo:d={length:.6f}",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                dest,
            ],
            timeout=60,
        )

    def _concat_two(self, a: str, b: str, dest: str) -> None:
        list_file = dest + ".txt"
        with open(list_file, "w", encoding="utf-8") as fh:
            for p in (a, b):
                escaped = os.path.abspath(p).replace("\\", "/").replace("'", r"'\''")
                fh.write(f"file '{escaped}'\n")
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                dest,
            ],
            timeout=180,
        )

    def _xfade(self, a: str, b: str, overlap: float, dest: str) -> None:
        dur_a = float(probe_media(a).get("duration_seconds") or 0)
        offset = max(0.0, dur_a - overlap)
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                a,
                "-i",
                b,
                "-filter_complex",
                (
                    f"[0:v][1:v]xfade=transition=fade:duration={overlap:.4f}:offset={offset:.4f}[v];"
                    f"[0:a][1:a]acrossfade=d={overlap:.4f}[a]"
                ),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                dest,
            ],
            timeout=180,
        )

    def _overlay(self, base: str, overlay: str, start: float, length: float, dest: str) -> None:
        end = start + length
        run(
            [
                which_ffmpeg("ffmpeg"),
                "-hide_banner",
                "-y",
                "-i",
                base,
                "-i",
                overlay,
                "-filter_complex",
                f"[0:v][1:v]overlay=0:0:enable='between(t,{start:.4f},{end:.4f})'[v]",
                "-map",
                "[v]",
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
            ],
            timeout=180,
        )

    def _mix_audio_onto(
        self,
        program: str,
        extra: list[tuple[str, float, dict[str, Any], dict[str, Any]]],
        dest: str,
        *,
        normalize: bool,
    ) -> None:
        cmd = [which_ffmpeg("ffmpeg"), "-hide_banner", "-y", "-i", program]
        for path, _start, _ev, _tr in extra:
            cmd.extend(["-i", path])
        n = 1 + len(extra)
        parts: list[str] = []
        mix_labels = ["[0:a]"]
        for i, (_path, start, ev, tr) in enumerate(extra, start=1):
            take = float(ev.get("take_offset_seconds") or 0.0)
            length = float(ev.get("length_seconds") or 0.0)
            gain = float(ev.get("gain") or tr.get("volume") or 1.0)
            delay_ms = max(0, int(start * 1000))
            filt = f"[{i}:a]atrim=start={take:.4f}"
            if length > 0:
                filt += f":end={take + length:.4f}"
            filt += f",asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms},volume={gain:.4f}[a{i}]"
            parts.append(filt)
            mix_labels.append(f"[a{i}]")
        mix = "".join(mix_labels) + f"amix=inputs={n}:duration=first:normalize=0[a]"
        if normalize:
            mix += ";[a]loudnorm=I=-16:TP=-1.5:LRA=11[an]"
            amap = "[an]"
        else:
            amap = "[a]"
        cmd.extend(
            [
                "-filter_complex",
                ";".join(parts + [mix]),
                "-map",
                "0:v",
                "-map",
                amap,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                dest,
            ]
        )
        run(cmd, timeout=180)
