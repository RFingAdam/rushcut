"""Resolve Windows known folders. Follows OneDrive Documents redirection.

``Path.home() / "Documents"`` and ``%USERPROFILE%\\Documents`` are the
wrong location when the user has redirected Documents to OneDrive.
VEGAS Pro loads Application Extensions from the *real* Documents folder
(the same path `[Environment]::GetFolderPath('MyDocuments')` returns).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# CSIDL_PERSONAL = My Documents (respects Known Folder redirection)
CSIDL_PERSONAL = 5
SHGFP_TYPE_CURRENT = 0


def _sh_get_folder_path(csidl: int) -> Path | None:
    """SHGetFolderPathW; None when not Windows or the call fails."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        hr = ctypes.windll.shell32.SHGetFolderPathW(
            None, csidl, None, SHGFP_TYPE_CURRENT, buf
        )
        if hr == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        return None
    return None


def my_documents() -> Path:
    """User Documents folder, following Known Folder / OneDrive redirection.

    Override with VEGAS_DOCUMENTS when you need a test or non-standard path.
    """
    override = os.environ.get("VEGAS_DOCUMENTS")
    if override:
        return Path(override)
    known = _sh_get_folder_path(CSIDL_PERSONAL)
    if known is not None:
        return known
    return Path.home() / "Documents"


def vegas_application_extensions_dir() -> Path:
    return my_documents() / "Vegas Application Extensions"


def vegas_script_menu_dir() -> Path:
    return my_documents() / "Vegas Script Menu"
