---
name: rushcut-mcp
description: Drive an editor-agnostic MCP video-edit bridge (FFmpeg, VEGAS Pro, or DaVinci Resolve Studio). Use when probing media, stitching a recording session, detecting overlay presence, building a timeline, trimming, mixing audio, burning captions, reframing, rendering, planning a long-form story format, measuring narration pacing, or checking a render.
---

# rushcut-mcp

MCP tools talk to whichever backend `DIRECTOR_EDITOR` selects (`ffmpeg`,
`vegas`, `resolve`, `otio`). Analysis tools never need an NLE.

Call `list_editors` / `editor_info` / `editor_capabilities` if you are
unsure what is connected. Call `ensure_host` before live VEGAS or Resolve
edits.

## Typical flow

1. `probe_media` on the source. Split recordings: `stitch_session` groups
   files by gap, remuxes unfinalized MKVs, and returns an offset map.
   `cut_media` / `transcribe_media` / `detect_region_presence` accept
   session time via `offset_map` + `session_start` / `session_seconds`.
2. Optional: `detect_scenes`, `analyze_audio`, `transcribe_media`
   (word timestamps; optional `diarize` or injected speaker `turns`).
   `detect_region_presence(path, box, mode=auto|face|activity)` for a cam
   or overlay box (CPU Haar, or variance/edge activity if OpenCV is missing).
3. Decide in/out points yourself (this skill does not rank clips).
   Validate them with `check_output` / `rushcut check` before render.
   Pass `must_include: [{t, pre, post, id}]` so required events stay on
   the cut timeline.
4. `cut_media` / `concat_media` / `reframe_media` for file-level jobs, **or**
   `apply_timeline(edit_json, output)` for a multi-segment edit.json
   (J/L cuts, zoom, speed ramps, layouts, overlays — see docs/EDIT_JSON.md), **or**
   build a timeline:
   - `new_project` or `open_project`
   - `set_project_video` if the frame size should change
   - `add_track` → `place_media` / `add_video_event` / `add_audio_event`
   - `trim_event`, `move_event`, `set_event_fades`, `set_motion_keyframes`
   - `apply_crossfade` on joins; `apply_ducking` or `add_music_bed` for music under voice
   - `insert_audio` / `replace_audio_span` for VO lines; `bleep_spans` with caption `masked_spans`
   - `loop_fades` only for an explicit fade-to-black sting — never on a looping Short (`check_loop` fails a forced fade)
   - `generate_captions` (chunking, mask, `active_word`) then `burn_captions`, or `add_title`
   - `audio_cleanup` for hum / tonal whine; `master_audio` before delivery
5. `render_with_preset` (`1080p`, `vertical`, `4k`, optional `master=true`) or `render_start`.
   Encodes write `<name>.partial.mp4` and rename only after a full-decode
   QA. Honor `RUSHCUT_OUTPUT_ROOTS` / profile `output_roots` — never
   render to `C:` when a D: edit root is configured.
6. `verify_output` on the file (`moov_ok`, `decode_errors`, `md5`, `fps_exact`,
   LUFS, `head_black_s`). `frame_check` writes labelled frames + a sheet.
   Then
   `check_output` (or `rushcut check`)
   with the profile, transcript, and cut list. Speech-boundary, required
   features, and source-preference failures are not silent.
   `check_media` on the render: integrated LUFS, true peak, duration, fps,
   and a speech-boundary re-check (chopped words at joins).
   `check_loop` / `rushcut check-loop` for Shorts: SSIM of the last frames
   vs frame 1, audio-level continuity across the wrap, and no forced
   fade-to-black / fade-to-silence at the edges.
   After a re-render of an approved cut, `rushcut check --parity APPROVED
   CANDIDATE --json parity.json` (optional `--edit-region`, `--region`,
   `--time-map`, `--presence-region`, `--with-loop`, `--with-loudness`).
   Exit 0/1. Do not redirect stdout for the JSON.

## Story formats & pacing

For long-form gameplay stories:

1. `list_story_formats` (`summary_only=true` to browse) and pick a format id.
2. Log beats from the footage yourself: `[{t_sec, label, value?}]`. Give a
   value to every beat that closes a step (money, level, price, count), and
   mark the payoff `cold_open: true` when the largest value is not it.
3. `plan_story_edit(format_id, beats, target_minutes, slots={game, role, item, ...})`.
   Fill anything in `missing_slots`, then cut to the plan: 2-3 s
   flash-forward at 0:00, goal and plan by 0:15, every section ends on its
   value, and every `vo_slots` gap is narrated or trimmed.
4. After the cut, `chapters_from_beats` with edited-timeline times gives the
   description chapter block (first 0:00, 3+, each 10 s+).
5. `measure_pacing` on the render (`model_size="large-v3"` when comparing
   videos, `promise_keywords` for the title's promise). Gate: no dead
   stretches of 8 s+, 110+ wpm on narrated stories, promise by 0:15. Fix and
   re-render until `gate_passed` is true.

These tools never rank footage; you choose the beats.

## Guardrails

- Times are seconds, not ticks.
- Soft failures are `{ok: false, error}` — never assume success.
- Do not cut inside a word. Snap in/out to silence with the profile pad
  (`quality.speech_boundary.pad_seconds`). `check_seams` / `snap_cuts`
  score the mic envelope and move voiced seams to the nearest dip
  without dropping covered words. Keep a conversation partner's
  reply if it starts within `turn_completeness.reply_window_seconds`.
- Do not let a speed ramp (speed ≠ 1) cover a word. `check_speed_ramps`
  flags each 1/2/4/6x step; pad defaults to 80 ms. Bleeped words count.
- Prefer `local_recording` over `platform_vod` when both exist.
- Honor `required_features` from `edit_profile.yaml`; record what you
  applied on the edit (`applied_features`) so `check_output` can verify.
- Honor `output_roots` / `RUSHCUT_OUTPUT_ROOTS`. A path outside those
  roots returns `{ok:false, error:"output outside allowed roots"}`.
- `find_highlights`, `apply_vertical_short`, and `propose_edit` are removed
  stubs. Do not call them for real work.
- VEGAS extension mode needs `%LOCALAPPDATA%\vegas-director-mcp\token`.
  Do not commit it.
- Resolve live control needs Studio 21.1+ and External scripting = Local.
  Free Resolve: use `ffmpeg` or `otio` instead.

See [docs/FFMPEG.md](../../docs/FFMPEG.md), [docs/SETUP.md](../../docs/SETUP.md),
[docs/RESOLVE.md](../../docs/RESOLVE.md).
