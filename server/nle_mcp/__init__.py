"""Deprecated alias for :mod:`rushcut` (``nle-mcp`` was renamed to Rushcut MCP).

``import nle_mcp`` and ``python -m nle_mcp`` keep working for one release and
emit a DeprecationWarning. New code should use ``rushcut``.
"""
from __future__ import annotations

from rushcut import main, mcp  # noqa: F401
from rushcut._compat import install_alias

install_alias(__name__)
del install_alias
