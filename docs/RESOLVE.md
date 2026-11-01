# DaVinci Resolve backend

The same MCP tools can drive **DaVinci Resolve Studio** through Blackmagic's
official Python API (`DaVinciResolveScript` / `fusionscript`) instead of the
VEGAS C# host.

```
DIRECTOR_EDITOR=resolve
```

## Studio vs free — this is a hard line

The live Resolve backend **only works on Resolve Studio 21.1+** with

**Preferences → System → General → External scripting = Local**

(or Network, if you really want LAN access to port 1144).

| Edition / version | Scripting? | This backend |
|---|---|---|
| **Resolve Studio 21.1+** + External scripting = Local | Yes (external `scriptapp`) | Supported |
| Resolve Studio before 21.1, External scripting = Local | External `scriptapp` already required Studio | Should work; untested here |
| **Free Resolve 21.1+** | **No Scripting API at all.** Blackmagic's 21.1 feature sheet lists `Scripting API: Studio`. They moved Python off the free edition. | `scripting_available: false` or `reason: free_edition` |
| Free Resolve before 21.1 | Workspace → Scripts (internal) only. External `scriptapp` was already Studio. | External attach fails; same error |

If the backend cannot attach, or it attaches and `GetProductName()` does not
contain `Studio`, every live tool returns `{ok: false}` with:

- `studio_required: true`
- `reason`: `scripting_available` / `free_edition` / `scripting_unavailable`
- `hint` pointing at `DIRECTOR_EDITOR=otio` (free-friendly export)

CI and unit tests **never** talk to a live Resolve. They inject a fake
`DaVinciResolveScript` object.

### Studio-only API extras (even on Studio)

These exist on Studio and are **not** in the free edition (and were Studio
before 21.1 too):

| API | MCP tool | Notes |
|---|---|---|
| `Timeline.CreateSubtitlesFromAudio` | `resolve_create_subtitles` | Dialogue → subtitle track |
| `TimelineItem.SmartReframe` | `resolve_smart_reframe` | Auto 9:16 reframing |
| `SetVoiceIsolationState` | `resolve_set_voice_isolation` | Fairlight voice isolation |

They are not required for the VEGAS-shaped tools (place / trim / render).

## Windows / macOS / Linux setup

Resolve must already be **running** (or `ensure_host` will try to launch it).
The MCP server uses *your* Python, not Resolve's bundled interpreter, so you
still need the official module on `PYTHONPATH`.

### Environment

| Variable | Typical value |
|---|---|
| `DIRECTOR_EDITOR` | `resolve` |
| `RESOLVE_SCRIPT_API` | See OS table |
| `RESOLVE_SCRIPT_LIB` | See OS table |
| `PYTHONPATH` | `$RESOLVE_SCRIPT_API/Modules` |
| `RESOLVE_EXE` | Override the Resolve binary |
| `RESOLVE_LAUNCH_WAIT` | Seconds to wait after launch (default 60) |
| `RESOLVE_NOGUI` | `1` to pass `-nogui` (Studio headless; scripting still works) |

### Default paths (from Blackmagic's Scripting README)

**Windows**

```
RESOLVE_SCRIPT_API=%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
PYTHONPATH=%PYTHONPATH%;%RESOLVE_SCRIPT_API%\Modules\
```

Binary: `C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe`

**macOS**

```
RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
```

**Linux**

```
RESOLVE_SCRIPT_API=/opt/resolve/Developer/Scripting
RESOLVE_SCRIPT_LIB=/opt/resolve/libs/Fusion/fusionscript.so
```

ISO / older layouts use `/home/resolve/...` instead of `/opt/resolve/...`.

Resolve 21.1+ also ships `ResolvePython` (Windows:
`C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolvePython\ResolvePython.exe`)
where `import DaVinciResolveScript` works without env vars. The MCP server
still uses the venv Python; set the three variables above.

### MCP client snippet (Windows)

```json
{
  "mcpServers": {
    "rushcut-mcp": {
      "command": "C:\\path\\to\\rushcut\\server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rushcut"],
      "cwd": "C:\\path\\to\\rushcut\\server",
      "env": {
        "DIRECTOR_EDITOR": "resolve",
        "RESOLVE_SCRIPT_API": "C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting",
        "RESOLVE_SCRIPT_LIB": "C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll",
        "PYTHONPATH": "C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules"
      }
    }
  }
}
```

## Tool mapping (VEGAS RPC → Resolve)

Legend: ✅ 1:1 · 🟨 partial / different metaphor · ❌ unsupported

| MCP / RPC | Resolve API | Status |
|---|---|---|
| `ping`, `host.info`, `editor_info` | `scriptapp`, `GetProductName`, `GetVersionString` | ✅ (+ Studio gate) |
| `project.get_state` | `GetCurrentTimeline`, `GetItemListInTrack` | ✅ (video tracks first, then audio; 0-based flat index) |
| `project.get_selected_events` | `GetCurrentVideoItem` | 🟨 single playhead item, no multi-select API |
| `project.save` | `SaveProject` / `ExportProject` (`.drp`) | ✅ |
| `project.new` / `project.open` | `CreateProject`, `LoadProject`, `ImportProject` | 🟨 Resolve is database-first; `.drp` imports, other paths are project names |
| `project.set_video` | `SetSetting(timelineResolution*)` | ✅ |
| `track.add` | `Timeline.AddTrack` | 🟨 always appends; insert `index` is not honored |
| `track.set_props` | `SetTrackName`, `SetTrackEnable` | 🟨 no solo / track volume |
| `track.set_composite_level` | clip `Opacity` on every item | 🟨 no track CompositeLevel |
| `track.clear_events` | `DeleteClips` | ✅ |
| `media.import` / `media.place` / `event.add_*` | `ImportMedia`, `AppendToTimeline({clipInfo})` | ✅ |
| `media.list` / `media.replace` | folder walk, `ReplaceClip` | ✅ |
| `media.set_alpha` | `SetClipProperty("Alpha mode")` | 🟨 name varies by version |
| `event.trim` / `move` / `delete` | delete + re-append with new `recordFrame` / source frames | ✅ |
| `event.set_motion` | inspector `ZoomX/Y`, `Pan`, `Tilt` | 🟨 last keyframe only; no curve API |
| `event.set_fades` | — | 🟨 no fade-handle API; overlap still works |
| `event.set_opacity` | `SetProperty("Opacity")` (0–100) | ✅ |
| `event.set_gain` | Fairlight | ❌ |
| `event.add_title` | `InsertFusionTitleIntoTimeline` | 🟨 inserted at playhead |
| `envelope.*` | timeline markers (`customData=director-envelope`) | 🟨 |
| `transport.seek` | `SetCurrentTimecode` | ✅ |
| `transport.play` / `stop` | — | ❌ no Play/Stop in the public API |
| `render.list_templates` | built-in 1080/vertical/4k rows + `GetRenderPresetList` | ✅ |
| `render.start` / `render.status` | `SetRenderSettings`, `AddRenderJob`, `StartRendering`, `GetRenderJobStatus` | ✅ |
| `marker.*` | `Timeline.AddMarker` / `GetMarkers` | ✅ |
| `set_motion_keyframes` (composed) | last inspector Zoom/Pan | 🟨 no curve API |

Times stay in **seconds** on the MCP side. The backend converts with the
timeline frame rate (`GetSetting("timelineFrameRate")`). Resolve track
indices are 1-based internally; MCP `track_index` is 0-based, video then
audio then subtitle.

## Manual test checklist (Windows + Resolve Studio)

VEGAS is not required for this path. Free Resolve will fail on step 4 with
`reason: free_edition` or `scripting_unavailable` — that is expected.

1. Install / open **DaVinci Resolve Studio 21.1+** (not the free edition).
2. **Preferences → System → General → External scripting → Local.** Restart Resolve.
3. From `server\`:
   ```bat
   set DIRECTOR_EDITOR=resolve
   set RESOLVE_SCRIPT_API=%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
   set RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
   set PYTHONPATH=%RESOLVE_SCRIPT_API%\Modules
   .venv\Scripts\activate
   python -c "from rushcut.tools import ping, editor_info, get_project_state; print(ping()); print(editor_info()); print(get_project_state())"
   ```
4. `ping` / `editor_info` should show `ok: true`, `editor: resolve`,
   `studio: true`, product containing `Studio`.
5. `new_project(discard_unsaved=True)`, `set_project_video(1080, 1920)`,
   `add_track("video")`, `add_video_event` on a short local clip, `trim_event`,
   `set_motion_keyframes` (expect `partial: true`), `add_marker`.
6. `render_start` to a folder you can write, then `render_status` until
   `state=complete`. Check the MP4 exists.
7. Optional Studio extras: `resolve_create_subtitles`,
   `resolve_smart_reframe`, `resolve_set_voice_isolation`.
8. **Negative test (free / scripting off):** set External scripting to
   **None**, or run against free Resolve. `ping` must be `{ok: false}` with
   `studio_required: true` and must **not** mutate a project.
9. Free-friendly fallback: `set DIRECTOR_EDITOR=otio`, build a short
   timeline, `save_project` to `short.otio` and `short.fcpxml`, then in
   **free** Resolve: File → Import Timeline (or media-pool
   `ImportTimelineFromFile`) and confirm clips land.

Not affiliated with Blackmagic Design.
