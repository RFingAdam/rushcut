# Rushcut MCP

AI video editing over MCP. Point any agent at your footage and get edits from
FFmpeg, VEGAS Pro, DaVinci Resolve, or an OTIO/FCPXML timeline.

Rushcut is a [Model Context Protocol](https://modelcontextprotocol.io) server
for Claude Code, Claude Desktop, Cursor, or any other stdio MCP client. The same
tools drive whichever backend you pick, so a workflow does not change when you
switch editors. The FFmpeg backend is headless and needs no GUI editor or
license.

## Install

Requires Python 3.11+ and `ffmpeg` and `ffprobe` on PATH.

```bash
pip install rushcut-mcp
pip install "rushcut-mcp[whisper]"   # adds local transcription
```

## Run

Add it to your MCP client as a stdio server:

```json
{
  "mcpServers": {
    "rushcut": {
      "command": "rushcut-mcp",
      "env": { "DIRECTOR_EDITOR": "ffmpeg" }
    }
  }
}
```

## Documentation

The [project README](https://github.com/RFingAdam/rushcut#readme) covers every
backend, the tool list, and troubleshooting. Report problems on the
[issue tracker](https://github.com/RFingAdam/rushcut/issues).

Rushcut is an independent project. It is not affiliated with or endorsed by
MAGIX or Blackmagic Design. See NOTICE.

MIT licensed. See LICENSE.
