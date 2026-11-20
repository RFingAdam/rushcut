"""Entry point for `python -m rushcut`.

No extra args starts the MCP server. Any argument goes to the CLI
(`python -m rushcut check ...`, `python -m rushcut check-loop ...`), so an
unknown subcommand is an argparse error rather than a silent server start.
"""
from __future__ import annotations

import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from .cli import main as cli_main

        raise SystemExit(cli_main())
    from . import main

    main()
