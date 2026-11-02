"""Entry point for `python -m rushcut`.

No extra args starts the MCP server. `python -m rushcut check ...` is the CLI.
"""
from __future__ import annotations

import sys

_CLI = {"check", "profile", "-h", "--help"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in _CLI:
        from .cli import main as cli_main

        raise SystemExit(cli_main())
    from . import main

    main()
