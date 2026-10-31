---
name: rushcut-mcp
description: Drive an editor-agnostic MCP video-edit bridge (FFmpeg, VEGAS Pro, or DaVinci Resolve Studio). Use when probing media, building a timeline, trimming, mixing audio, burning captions, reframing, rendering, planning a long-form story format, or measuring narration pacing.
---

# rushcut-mcp

MCP tools talk to whichever backend `DIRECTOR_EDITOR` selects (`ffmpeg`,
`vegas`, `resolve`, `otio`). Analysis tools never need an NLE.

Call `list_editors` / `editor_info` / `editor_capabilities` if you are
unsure what is connected. Call `ensure_host` before live VEGAS or Resolve
edits.

## Typical flow

1. `probe_media` on the source.
2. Optional: `detect_scenes`, `analyze_audio`, `transcribe_media`.
3. Decide in/out points yourself (this skill does not rank clips).
4. `cut_media` / `concat_media` / `reframe_media` for file-level jobs, **or**
   build a timeline:
   - `new_project` or `open_project`
   - `set_project_video` if the frame size should change
   - `add_track` → `place_media` / `add_video_event` / `add_audio_event`
   - `trim_event`, `move_event`, `set_event_fades`, `set_motion_keyframes`
   - `apply_crossfade` on joins; `apply_ducking` for music under voice
   - `generate_captions` then `burn_captions`, or `add_title`
5. `render_with_preset` (`1080p`, `vertical`, `4k`) or `render_start`.
6. `verify_output` on the file.

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
- `find_highlights`, `apply_vertical_short`, and `propose_edit` are removed
  stubs. Do not call them for real work.
- VEGAS extension mode needs `%LOCALAPPDATA%\vegas-director-mcp\token`.
  Do not commit it.
- Resolve live control needs Studio 21.1+ and External scripting = Local.
  Free Resolve: use `ffmpeg` or `otio` instead.

See [docs/FFMPEG.md](../../docs/FFMPEG.md), [docs/SETUP.md](../../docs/SETUP.md),
[docs/RESOLVE.md](../../docs/RESOLVE.md).
