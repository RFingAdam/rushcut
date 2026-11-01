"""Register rushcut-mcp as a stdio MCP server in Claude Desktop.

Uses the checkout that contains this script. Does not hard-code a user path.
Restart the Claude desktop app afterwards for the tools to appear.

    py -3 scripts\\register_claude.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        print("APPDATA is not set; this script is for Windows Claude Desktop.", file=sys.stderr)
        return 1

    cfg = Path(appdata) / "Claude" / "claude_desktop_config.json"
    if not cfg.is_file():
        print(f"Claude Desktop config not found: {cfg}", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parents[1]
    server_dir = repo_root / "server"
    venv_python = server_dir / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        print(
            f"Missing {venv_python}. Create the venv first:\n"
            f"  cd {server_dir}\n"
            "  py -3.11 -m venv .venv\n"
            "  .venv\\Scripts\\activate\n"
            "  pip install -r requirements.txt\n"
            "  pip install -e .",
            file=sys.stderr,
        )
        return 1

    backup = cfg.with_name(cfg.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy(cfg, backup)

    with cfg.open(encoding="utf-8") as fh:
        data = json.load(fh)

    servers = data.setdefault("mcpServers", {})
    servers["rushcut-mcp"] = {
        "command": str(venv_python),
        "args": ["-m", "rushcut"],
        "cwd": str(server_dir),
        "env": {
            "DIRECTOR_EDITOR": os.environ.get("DIRECTOR_EDITOR", "ffmpeg"),
            "VEGAS_HOST_TRANSPORT": "tcp",
            "VEGAS_HOST_ADDRESS": "127.0.0.1",
            "VEGAS_HOST_PORT": "8752",
            "PYTHONPATH": str(server_dir),
        },
    }
    # Keep the old key pointing at the same command so existing configs still work.
    servers.setdefault(
        "vegas-director",
        {
            "command": str(venv_python),
            "args": ["-m", "vegas_director_mcp"],
            "cwd": str(server_dir),
            "env": servers["rushcut-mcp"]["env"],
        },
    )
    with cfg.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"registered rushcut-mcp in {cfg}")
    print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
