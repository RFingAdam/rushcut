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
| `concat_media` | join files in order (stream-copy when codecs match; else quality preset) |
| `reframe_media` | cover-crop to width × height (optional pan/zoom) |
| `mix_audio` | mix audio streams |
| `normalize_audio` | single-pass ffmpeg `loudnorm` |
| `master_audio` | two-pass loudnorm, limiter, encode-aware true peak; copies video |
| `add_music_bed` / `bleep_spans` / `insert_audio` / `replace_audio_span` / `loop_fades` | file-level mix / censor / VO / loop fades |
| `apply_timeline` | render `edit.json` (segments, J/L cuts, effects) or a named audio op |
| `compose_layout` | `blur_fill` / `split` / `center` / `square` vertical compositor |
| `blur_media` | region blur / pixelate / fill |
| `track_circle` / `lens_zoom` | dark-ring tracker + circular magnify |
| `make_text_card` / overlay | title-card PNG + image/alpha overlay |
| `pick_frame` / `compose_thumbnail` / `frame_check` | sharpest frame, split thumbs, labelled contact sheet |
| `analyze_noise_floor` / `audio_cleanup` | band floors, notches, high-pass, optional adaptive tonal / mid-side |
| `generate_captions` / `burn_captions` | SRT + ASS (chunking, mask, karaoke); burn-in timeout/preset/CRF scale with program length |
| `detect_scenes` / `analyze_audio` / `transcribe_media` | grounding (word timestamps; optional diarize; `initial_prompt`) |
| `check_output` / `rushcut check` | speech-boundary, seams, speed ramps, must_include, required features, source preference, Shorts duration/loop |
| `check_parity` / `rushcut check --parity` | layout SSIM/MAD vs an approved cut; `--json PATH` writes the report |
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

Pass `master=true` (or set `master: true` on a preset) to run
`master_audio` on the rendered file. Defaults are -14 LUFS integrated,
-1.5 dBTP, LRA 11. The two-pass loudnorm + limiter path is pure ffmpeg.
The encode-aware true-peak loop (AAC 192k → decode → 4×-oversampled TP,
local gain reduction) uses numpy when you install
`pip install 'rushcut-mcp[analysis]'` and ffmpeg windowed limiting
otherwise. Video is copied (`-c:v copy`); only audio is rewritten.

## Caption style presets

`generate_captions(..., style=...)` writes SRT + ASS. Word timestamps
are grouped by `max_chars` / `max_words` / `max_gap` (split on gaps
greater than 0.5 s) and trailing punctuation; each cue is extended
~0.35 s but never into the next. `fix_map` and `keep_windows` run
before chunking. `mask_words` returns `masked_spans: [{start,end,word}]`
for `bleep_spans`. `active_word=true` writes a karaoke-style `\\k` ASS
preset (yellow active word). `initial_prompt` is forwarded to Whisper
when the tool transcribes `path`.

### 16:9 (`style="default"`)

- PlayRes 1920×1080, Arial 48
- White fill, black outline 4, shadow 2
- MarginV 40
- Default chunking: 42 characters, 4 words

### 9:16 (`style="vertical"`)

- PlayRes 1080×1920, Arial 64
- White fill, black outline 6, shadow 2
- MarginV 160
- Default chunking: 18 characters, 4 words, uppercase

## Loop fades

`loop_fades(path, in_s=0.2, out_s=0.2)` is an **optional** look: matching
fades at both ends, first 0.2 s up from silence and black, last 0.2 s
down to silence and black. It is not applied automatically.

Do **not** use it to make a Short wrap. Seamless loops need the last
frame to match frame 1 (and the audio wrap to stay level). A forced
fade-to-black / fade-to-silence is a seam; `check_loop` /
`rushcut check-loop` fail it. Call `loop_fades` only when you want that
sting, not when the file should loop.

## Hum cleanup

`analyze_noise_floor` returns per-window band floors (dB) and tonal
lines (frequency, prominence, mid vs side). `audio_cleanup` takes a
`notches` list, `hp_hz`, `adaptive_tonal`, `ms_mode`, and
`max_reduction_db`. STFT is streamed in chunks so a 3-hour file does
not keep every frame in RAM. Optional DeepFilterNet voice separation
is `pip install 'rushcut-mcp[denoise]'`. Set `comparison_clip=true`
for a 60 s before/after wav.
