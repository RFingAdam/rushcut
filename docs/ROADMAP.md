# Roadmap

## Phase 0 — Scaffold
- Done: repo structure, protocol, license.

## Phase 1 — First real round-trip (complete)
- Host: TCP `127.0.0.1:8752` only, WinForms UI-thread marshal, SoftFail `ok:false`.
- Methods: ping / project.get_state (+ media_path, take offsets) / save /
  track.add / media.import / media.place / event.add_video|audio / trim
  (+ take_offset) / move / delete / transport.*.
- MCP: matching FastMCP tools; default transport TCP.

## Phase 2 — Magix FAQ editorial primitives (complete)
- `event.set_motion` — VideoMotion pan/crop keyframes (Bounds crop-zoom).
- `event.set_fades` — FadeIn/Out Length + Smooth curve; optional Dissolve;
  reciprocal_curve for overlaps.
- `event.set_opacity` — FadeIn.Gain.
- `event.add_title` — Titles & Text generator + OFX Text RTF (PNG overlays
  if the generator is missing).
- `track.set_composite_level` — track opacity.
- `envelope.set_points` — Volume / CompositeLevel create-or-update points.
- `project.get_selected_events` — selected track/event indices.
- `render.start` — best-effort RenderArgs or SoftFail → File > Render As.

## Phase 3a — Host-side project / media / track tools (complete)
- `project.new` / `project.open` with save-or-discard.
- `media.list` / `media.replace` / `media.set_alpha`.
- `track.set_props` / `track.clear_events` / `track.add` insert `index`.
- `envelope.clear`, `event.set_gain`, `render.list_templates`.
- v2 host: `StartHeadless` + Application Extension auto-start + runtime token.

## Phase 3b — Media grounding + edit helpers (complete)
Pure Python + ffmpeg (no VEGAS):

- `probe_media` (ffprobe)
- `detect_scenes` (ffmpeg scene score)
- `analyze_audio` — windowed RMS/peak spikes
- `transcribe_media` — optional `faster-whisper`; CUDA if ctranslate2 sees a GPU, else CPU int8; soft-fail if the extra is missing
- `generate_captions` / `burn_captions` — generic SRT + ASS and ffmpeg burn-in
- File-level `cut_media`, `concat_media`, `reframe_media`, `mix_audio`, `normalize_audio`

Host-backed helpers:

- `apply_crossfade`, `apply_ducking`
- `project.set_video` — output frame size / fps

## Phase 4 — Render & delivery (complete)
- Named presets: `vertical` (1080×1920), `1080p`, `4k` via `render_with_preset`
- `render.start(async=true)` uses documented `Vegas.BeginRender` + `RenderProgress` / `RenderFinished`
- `render_status` / `wait_for_render` poll from Python (never sit on the UI-marshal wait)
- `verify_output` (ffprobe: duration, resolution, audio)
- Sidecar `<output>.edit.json`: length, hook start, caption style, zoom count, music, template

## Dual-mode host (complete, documented against Pro 22 help)
VEGAS Pro 22 does **not** ship a GUI-less render farm. Official switches are
`/NOLOGO`, `/OPEN`, `/RUNSCRIPT` (`/SCRIPT`), `/SCRIPTARGS`, `/CMDMODULE`.
Application Extensions load at startup from the Known Folder
`Documents\Vegas Application Extensions` (OneDrive-aware).

Same MCP tools pick a mode and set `used_mode`:

- `live` — TCP already up
- `launched_extension` / `launched_runscript` — `ensure_host` starts VEGAS
- `oneshot_runscript` — generated `.cs` + sibling `.job.json`

See [skills/rushcut/SKILL.md](../skills/rushcut/SKILL.md).

## Non-goals (for now)
- Full VEGAS UI parity — editing/automation surface, not remote desktop.
- Multi-user/remote collaboration — single local VEGAS instance only.
- True GUI-less VEGAS.

## Multi-editor adapter

- Shared backend protocol under `server/rushcut/backends/`.
- FFmpeg headless backend (cut, concat, mix, reframe, render).
- VEGAS default unchanged for existing installs.
- Resolve Studio 21.1+ live backend (free edition has no API).
- OTIO / FCPXML / MLT as plain timeline I/O.
- Next live candidate: Premiere Pro UXP — [docs/EDITORS.md](EDITORS.md).
