# Wire Protocol

The MCP server and the VEGAS script host talk JSON-RPC 2.0, one JSON
object per line, over TCP.

## Transport

The host in `host/VegasDirectorHost.cs` binds **TCP loopback only**:
`127.0.0.1:8752`. Do not bind this off localhost. If the MCP server must
run on another machine, use an SSH tunnel or VPN to that loopback port.

The Python client (`server/rushcut/host_client.py`) defaults
to TCP via `VEGAS_HOST_TRANSPORT` (default `tcp`). It still contains a
Windows named-pipe path (`\\.\pipe\vegas-director`). The current host
does not create that pipe; `VEGAS_HOST_TRANSPORT=pipe` will fail against
this tree.

The host accepts one client at a time and handles that connection until
it closes, then accepts the next. The Python client opens a **new**
TCP connection per RPC call.

## Authentication

- **Script Menu mode** (`mode=script`): no token. The host dialog is an
  explicit, local, user-started listener.
- **Extension mode** (`mode=extension`): every request must include a
  top-level `"token"` string matching
  `%LOCALAPPDATA%\vegas-director-mcp\token`. The file is created on first
  extension start (32 random bytes, hex). Missing/wrong token is JSON-RPC
  error `-32001`. The Python client sends the token automatically when
  the file or `VEGAS_HOST_TOKEN` is present.

Do not commit the token file. It is listed in `.gitignore`.

## Message shape

```json
{"jsonrpc": "2.0", "id": 1, "method": "project.get_state", "params": {}}
```

```json
{"jsonrpc": "2.0", "id": 1, "result": {"ok": true, "length_seconds": 0, "video_track_count": 0, "audio_track_count": 0, "tracks": [], "events": []}}
```

Parse failures and unknown methods use a JSON-RPC **error** object:

```json
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found: render.start"}}
```

Almost every VEGAS-side failure is a JSON-RPC **result** with
`ok: false` so the script host does not crash the editor:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"ok": false, "error": "No active VEGAS project"}}
```

## Methods implemented in this tree

| Method | Params (common) | Notes |
|---|---|---|
| `ping` | — | `{ok, host, mode}` |
| `host.info` | — | `{ok, version, mode, requests, started, v2}` |
| `project.get_state` | — | length, track counts, tracks, events |
| `project.get_selected_events` | — | selected `track_index` / `event_index` |
| `project.save` | `path?` | omit `path` to save in place |
| `project.new` | `save_current_to?`, `discard_unsaved?` | one of the two required |
| `project.open` | `path`, `save_current_to?`, `discard_unsaved?` | |
| `track.add` | `type` (`video`\|`audio`), `name?`, `index?` | |
| `track.set_composite_level` | `track_index`, `level` | |
| `track.set_props` | `track_index`, `name?`, `mute?`, `solo?`, `volume?` | |
| `track.clear_events` | `track_index` | |
| `media.import` | `path` | media pool only |
| `media.place` | `path` or `media_path`, `track_index`, `start_seconds?`, `length_seconds?` | video vs audio from track type; `length_seconds=-1` = full media |
| `media.list` | — | pool paths, offline, alpha, length |
| `media.replace` | `old_path`, `new_path` | `Media.ReplaceWith` |
| `media.set_alpha` | `alpha`, `path?` or `path_contains?` | `VideoAlphaType` |
| `event.add_video` | `track_index`, `media_path` or `path`, `start_seconds?`, `length_seconds?` | video track required |
| `event.add_audio` | same | audio track required |
| `event.add_title` | `track_index`, `start_seconds`, `length_seconds`, `text`, `preset?` | |
| `event.trim` | `track_index`, `event_index`, `start_seconds?`, `length_seconds?`, `take_offset_seconds?` | omitted fields unchanged |
| `event.move` | `track_index`, `event_index`, `start_seconds` | |
| `event.delete` | `track_index`, `event_index` | |
| `event.set_motion` | `track_index`, `event_index`, `keyframes`, `reset?` | |
| `event.set_fades` | `track_index`, `event_index`, `fade_in_seconds?`, `fade_out_seconds?`, `dissolve?`, `curve?`, `reciprocal_curve?` | |
| `event.set_opacity` | `track_index`, `event_index`, `opacity` | |
| `event.set_gain` | `track_index`, `event_index`, `gain?` / `gain_db?`, `normalize?` | audio |
| `envelope.set_points` | `track_index`, `envelope_type`, `points` | |
| `envelope.clear` | `track_index`, `envelope_type` | |
| `transport.play` | — | |
| `transport.stop` | — | |
| `transport.seek` | `seconds` | cursor position |
| `render.list_templates` | `renderer_contains?`, `template_contains?`, `limit?`, `offset?` | `limit=0` (host default) = unlimited; `truncated` when a positive limit was hit |
| `render.start` | `output_path`, `template_name?`, `renderer_name?`, `start_seconds?`, `length_seconds?`, `async?` | default `async=true` → `BeginRender` + `job_id` |
| `render.status` | `job_id?` | `{state, percent, output_path}` |
| `project.set_video` | `width?`, `height?`, `frame_rate?` | |

MCP tool names are the Python functions in
`server/rushcut/__init__.py` (`get_project_state`,
`add_track`, `place_media`, `new_project`, …). They are thin wrappers
over the RPC names above. See [API_COVERAGE.md](API_COVERAGE.md).

Python-only (not host RPC): `probe_media`, `detect_scenes`, `analyze_audio`,
`transcribe_media`, `generate_captions`, `burn_captions`, `cut_media`,
`concat_media`, `reframe_media`, `mix_audio`, `normalize_audio`,
`verify_output`. Helpers `apply_crossfade` / `apply_ducking` /
`render_with_preset` compose the RPCs above. The FFmpeg backend implements
the same RPC names and encodes on `render.start`.

## Threading

`ScriptPortal.Vegas` is not thread-safe. The TCP listener runs on a
background thread. Each VEGAS call is marshaled to the UI thread
with a WinForms control (`Control.BeginInvoke`). Ordinary calls wait
60s; `project.save` / `project.new` / `project.open` / `render.start`
wait 30 minutes. Callers do not marshal.

VEGAS wraps a Script Menu run in an `UndoBlock` automatically.
Application Extensions do **not**. Project mutations from extension
mode (`track.add`, `project.new`, `project.set_video`, event/media
edits, …) are therefore wrapped in `new UndoBlock("Vegas Director: …")`
on that UI thread. Without it, VEGAS raises COM `E_UNEXPECTED`
(`0x8000FFFF`) even though reads succeed. Nested undo blocks are valid
in script mode. Render / transport / save / list calls are not wrapped.

## Timecodes

Positions and durations on the wire are **seconds** (`start_seconds`,
`length_seconds`, `seconds`). The host converts with
`Timecode.FromSeconds` / `ToMilliseconds`. The MCP tools do not parse
`HH:MM:SS` and do not convert ticks.

## Error codes

| Code | When |
|---|---|
| `-32700` | JSON parse failure (JSON-RPC error) |
| `-32601` | Unknown method (JSON-RPC error) |
| `-32001` | Extension mode: missing or wrong token (JSON-RPC error) |
| *(none)* | VEGAS/validation failures: result `{ok: false, error}` |

## Multi-editor

`DIRECTOR_EDITOR=vegas|resolve|otio` picks a backend. VEGAS still uses
this TCP protocol unchanged. Resolve and OTIO implement the same method
names in-process (no TCP). Resolve refusals for free / no-scripting use
result `{ok: false, studio_required: true, reason: ...}` rather than a
JSON-RPC error code. Extra methods: `marker.*`, `editor.capabilities`,
`resolve.create_subtitles`, `resolve.smart_reframe`,
`resolve.set_voice_isolation`.
