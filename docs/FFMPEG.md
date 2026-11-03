# FFmpeg backend (headless)

No GUI NLE. The MCP server keeps an in-memory timeline and encodes with
`ffmpeg` on `render.start`.

```
DIRECTOR_EDITOR=ffmpeg
python -m rushcut
```

Requires `ffmpeg` and `ffprobe` on PATH (`FFMPEG_BIN` / `FFPROBE_BIN`
override the executable).

## What you can do

Same timeline tools as VEGAS and Resolve: `new_project`, `add_track`,
`place_media` / `add_video_event` / `add_audio_event`, `trim_event`,
`set_event_fades`, `set_motion_keyframes`, `set_event_gain`, `add_title`,
`apply_crossfade`, `apply_ducking`, `render_start` / `render_with_preset`.

`length_seconds=-1` probes the file and uses the remaining duration.

`project.save` still writes `.otio` / `.fcpxml` / `.mlt` (plain timeline
I/O). Rendering is a separate step.

File-level tools (no timeline required):

| Tool | Role |
|---|---|
| `probe_media` | duration, size, fps, audio; flags unfinalized containers |
| `stitch_session` | group recordings by gap, remux unfinalized MKVs, concat, offset map |
| `session_lookup` | map session time onto a source file |
| `detect_region_presence` | per-second overlay/face presence in a box |
| `cut_media` | trim to a time range (also accepts session time) |
| `concat_media` | join files in order |
| `reframe_media` | cover-crop to width × height (optional pan/zoom) |
| `mix_audio` | mix audio streams |
| `normalize_audio` | ffmpeg `loudnorm` |
| `generate_captions` / `burn_captions` | SRT + ASS, optional burn-in |
| `detect_scenes` / `analyze_audio` / `transcribe_media` | grounding (word timestamps; optional diarize) |
| `check_output` / `rushcut check` | speech-boundary, seams, speed ramps, must_include, required features, source preference, Shorts duration/loop |
| `check_seams` / `snap_cuts` | envelope dBFS at each cut, snap to nearest dip |
| `check_speed_ramps` | flag speed ≠ 1 steps that cover a word |
| `check_loop` / `rushcut check-loop` | SSIM last vs first, audio wrap |
| `check_media` / `rushcut check-media` | LUFS, true peak, duration, fps, chopped words on the render |
| `export_review_pack` / `rushcut export-review-pack` | 720p proxies, contact sheets, QA.md, optional chunks |
| `fits_cap` / `rushcut fits-cap` | predict output duration from a segment spec |

## Example (headless cut)

```python
from rushcut.tools import (
    new_project, add_track, add_video_event, trim_event,
    set_project_video, render_start, wait_for_render,
)

new_project(discard_unsaved=True)
set_project_video(1920, 1080, 24)
add_track("video", "V1")
add_video_event(0, r"/path/to/clip.mp4", 0.0, -1)
trim_event(0, 0, length_seconds=5.0, take_offset_seconds=12.0)
# Prefer a configured root (RUSHCUT_OUTPUT_ROOTS / output_roots) over C:\out.
started = render_start(r"/path/to/edits/out.mp4", async_render=True)
wait_for_render(started["job_id"])  # decode-check, write out.qa.json, then rename
```

Transport (`play` / `stop` / `seek`) is unsupported — there is no GUI.

Render templates advertised by this backend are ffmpeg size presets
(`Vertical 1080x1920`, `Internet HD 1080p`, `4K UHD`) so
`render_with_preset` works the same way as on VEGAS and Resolve.
