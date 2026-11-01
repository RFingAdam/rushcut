# Setup (VEGAS Pro 22 on Windows)

You need a licensed VEGAS Pro 22 install. The MCP server talks to a C#
host that runs **inside** VEGAS over TCP `127.0.0.1:8752`.

Host tools need VEGAS. `probe_media`, `detect_scenes`, `analyze_audio`,
`transcribe_media`, `generate_captions`, `cut_media`, `concat_media`,
`reframe_media`, and `verify_output` are ffmpeg/Python and work with VEGAS
closed. Headless editing with no VEGAS: [FFMPEG.md](FFMPEG.md).

## 1. Script host

Pick **one**. The extension is the default path; the Script Menu copy is
the fallback if you do not want to compile a DLL.

### A. Application Extension (auto-start, preferred)

VEGAS Pro 22 loads compiled `ICustomCommandModule` assemblies from
`Documents\Vegas Application Extensions` (and a few AppData/ProgramData
siblings) at startup and keeps them loaded for the process lifetime.
That is documented under *Using Scripting → VEGAS Pro extensions* and the
Application Extensions FAQ.

**Documents is the Windows Known Folder**, not always
`%USERPROFILE%\Documents`. If Documents is redirected to OneDrive (common
on Windows 11), VEGAS reads e.g.
`C:\Users\<you>\OneDrive\Documents\Vegas Application Extensions`.
`host\build.ps1` uses `[Environment]::GetFolderPath('MyDocuments')` and
the Python launcher uses `SHGetFolderPathW` so the DLL lands where VEGAS
looks. Do not copy to `%USERPROFILE%\Documents\...` unless that is the
same folder.

1. Close VEGAS if it is open (the DLL is locked while loaded).
2. From the repo root:

   ```bat
   powershell -ExecutionPolicy Bypass -File host\build.ps1
   ```

   `build.ps1` builds one `VegasDirector.dll` per installed VEGAS, each
   compiled against that version's own `ScriptPortal.Vegas.dll` (it is
   strong-named per release: 22.0.0.250 vs 2027.0.0.31):

   | VEGAS | Install dir | Extension folder |
   |---|---|---|
   | Pro 22 | `C:\Program Files\VEGAS\VEGAS Pro 22.0` | `%LOCALAPPDATA%\VEGAS Pro\22.0\Application Extensions` |
   | Boris FX Pro 2027 | `C:\Program Files\BorisFX\Vegas Pro 2027` | `%LOCALAPPDATA%\VEGAS Pro\2027.0\Application Extensions` |

   `-Version 22` or `-Version 2027` builds just one. `VEGAS_DIR` +
   `VEGAS_APPVER` override a non-default install. The shared
   `<MyDocuments>\Vegas Application Extensions\` folder is scanned by every
   version, so the script no longer installs there and renames an old copy
   to `*.shared-legacy.disabled`. It also copies the `.cs` host into
   `<MyDocuments>\Vegas Script Menu\` (Known Folder, follows OneDrive) as a
   manual fallback.
3. Start **VEGAS** (2027 or 22). The host should already be listening — no
   *Tools > Scripting* step.
4. Optional status pane: *View > Extensions > Vegas Director Host*.
5. The host writes a per-user token to
   `%LOCALAPPDATA%\vegas-director-mcp\token` (created on first start,
   readable only by this Windows user). The MCP client sends it
   automatically. **Do not commit that file.**

If `csc.exe` is missing, use Script Menu mode below.

### B. Script Menu fallback

No Visual Studio project. VEGAS compiles `VegasDirectorHost.cs` when you
run it from the Scripting menu.

1. Copy `host/VegasDirectorHost.cs` into a Script Menu folder:
   - `<MyDocuments>\Vegas Script Menu\` (preferred; same Known Folder as
     above — OneDrive-redirected Documents if that is how this PC is set
     up), or
   - `C:\Program Files\VEGAS\VEGAS Pro 22.0\Script Menu\` (often needs
     admin), or
   - A user-writable folder listed under
     *Options > Preferences > Folders > Script Menu*
2. Open **VEGAS Pro 22** and create or open a project. The host refuses
   most calls if no project is active (`ok: false`).
3. *Tools > Scripting > VegasDirectorHost*
   - If the item is missing, *Tools > Scripting > Rescan Script Menu Folder*
     or restart VEGAS.
4. Leave the dialog open. Closing it stops the listener.
5. If the extension is already listening, the script exits with a
   "already running" message instead of binding a second port.

Script-menu mode does **not** require the token.

Logs for both modes: `%LOCALAPPDATA%\vegas-director-mcp\host.log`.

The current host does **not** open `\\.\pipe\vegas-director`. If you
re-copy the `.cs` file after a git pull, close the old dialog (or restart
VEGAS so the extension reloads) and try again.

## 2. MCP server (same Windows PC)

From a clone of this repo (`<clone>` is wherever you put it):

```bat
cd <clone>\server
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
set VEGAS_HOST_TRANSPORT=tcp
set VEGAS_HOST_ADDRESS=127.0.0.1
set VEGAS_HOST_PORT=8752
python -m rushcut
```

`python -m vegas_director_mcp` and `python -m nle_mcp` are deprecated aliases
(kept for one release; they warn on stderr). Re-run `pip install -e .` after
pulling the rename so the `rushcut` package is registered.
`pip install -e .` makes `python -m rushcut` work even when
the MCP client does not set a working directory. `requirements.txt`
matches `pyproject.toml` plus `pywin32` on Windows (only needed if you
force named-pipe transport, which the current host does not serve).

The process is stdio FastMCP. Point the MCP client at that command; do
not open a browser on port 8752 — that port is the **host**, not the
MCP server.

### Env vars

| Variable | Default | Role |
|---|---|---|
| `VEGAS_HOST_TRANSPORT` | `tcp` | `tcp` or `pipe` (pipe client exists; host does not listen) |
| `VEGAS_HOST_ADDRESS` | `127.0.0.1` | TCP host |
| `VEGAS_HOST_PORT` | `8752` | TCP port |
| `VEGAS_HOST_PIPE_NAME` | `vegas-director` | Unused unless transport is `pipe` |
| `VEGAS_HOST_TIMEOUT` | `60` | Seconds for ordinary RPCs (`project.save` / `new` / `open` / `render.start` use 1800) |
| `VEGAS_HOST_TOKEN` | *(file)* | Override the extension token. Otherwise the client reads `%LOCALAPPDATA%\vegas-director-mcp\token` when that file exists. |

### MCP client snippet

Cursor / Claude Desktop-style config. Use the venv interpreter and the
`server` directory as `cwd` (or rely on `pip install -e .`):

```json
{
  "mcpServers": {
    "rushcut-mcp": {
      "command": "C:\\path\\to\\rushcut\\server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rushcut"],
      "cwd": "C:\\path\\to\\rushcut\\server",
      "env": {
        "VEGAS_HOST_TRANSPORT": "tcp",
        "VEGAS_HOST_ADDRESS": "127.0.0.1",
        "VEGAS_HOST_PORT": "8752"
      }
    }
  }
}
```

Replace `C:\\path\\to\\rushcut` with your clone path.

Or, from the repo root after the venv exists:

```bat
py -3 scripts\register_claude.py
```

That writes the snippet above using this checkout's paths. Restart Claude
Desktop afterwards.

## 3. Smoke test

With the host listening (extension at VEGAS startup, or the Script Menu
dialog still open):

```bat
cd <clone>\server
.venv\Scripts\activate
set VEGAS_HOST_TRANSPORT=tcp
python -c "from rushcut.tools import ping, host_info, get_project_state; print(ping()); print(host_info()); print(get_project_state())"
```

Success looks like `ok: true` plus `mode` (`extension` or `script`) plus
`video_track_count` / `tracks`. A connection error means VEGAS is not
running, the host failed to start, or something else is bound to 8752.
`-32001 Unauthorized` in extension mode means the token file is missing
or `VEGAS_HOST_TOKEN` does not match.

### Analysis extras

Put `ffmpeg` + `ffprobe` on PATH (required for probe / scenes / loudness /
verify). Optional Whisper:

```bat
pip install -e ".[whisper]"
```

faster-whisper uses CUDA when ctranslate2 reports a GPU, otherwise CPU
int8. Missing extra is a soft `{ok:false}` — the rest of the pipeline still
runs.

Generic tool workflow: [skills/rushcut/SKILL.md](../skills/rushcut/SKILL.md).
Old name notes: [MIGRATION.md](MIGRATION.md).

`ffmpeg` is not required for the host-only smoke test above.

## 4. If the host is already running an old copy

**Extension:** close VEGAS. If you renamed `VegasDirector.dll` to
`VegasDirector.dll.disabled` while testing Script Menu mode, rename it
back to `.dll`. Re-run `host\build.ps1` (confirm the printed Documents
path is the OneDrive folder if this PC redirects Documents). Start VEGAS
again.

Extension-mode **writes** (new project, add track, set video, place an
event) need this rebuilt DLL: VEGAS does not wrap Application Extension
edits in an undo transaction the way it wraps *Tools > Scripting*, so
the host now creates an `UndoBlock` on the UI thread. Reads already
worked; old DLLs still fail writes with `E_UNEXPECTED`.

**Script Menu:**

1. Close the vegas-director-mcp status window (that stops the listener).
2. Replace `VegasDirectorHost.cs` in the Script Menu folder.
3. *Tools > Scripting > VegasDirectorHost* again.
