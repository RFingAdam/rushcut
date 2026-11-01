"""Locate DaVinciResolveScript / fusionscript and attach to a running Resolve.

External `scriptapp("Resolve")` is a Studio feature. Resolve 21.1+ removed
Python scripting from the free edition entirely. Preferences → System →
General → External scripting must be Local (or Network).

See docs/RESOLVE.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Official default locations from Blackmagic's Scripting README.
_WIN_API = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / (
    "Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting"
)
_WIN_LIB = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
_MAC_API = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
_MAC_LIB = Path(
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
)
_LINUX_API = Path("/opt/resolve/Developer/Scripting")
_LINUX_LIB = Path("/opt/resolve/libs/Fusion/fusionscript.so")
_LINUX_API_ALT = Path("/home/resolve/Developer/Scripting")
_LINUX_LIB_ALT = Path("/home/resolve/libs/Fusion/fusionscript.so")

RESOLVE_EXE_CANDIDATES = [
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe",
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve",
    "/opt/resolve/bin/resolve",
    "/home/resolve/bin/resolve",
]


def resolve_module_paths() -> list[Path]:
    """Folders that contain DaVinciResolveScript.py."""
    env = os.environ.get("RESOLVE_SCRIPT_API")
    paths: list[Path] = []
    if env:
        paths.append(Path(env) / "Modules")
        paths.append(Path(env))
    if sys.platform == "win32":
        paths.append(_WIN_API / "Modules")
    elif sys.platform == "darwin":
        paths.append(_MAC_API / "Modules")
    else:
        paths.append(_LINUX_API / "Modules")
        paths.append(_LINUX_API_ALT / "Modules")
    # Dedup while keeping order.
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def ensure_script_lib_env() -> None:
    """Set RESOLVE_SCRIPT_LIB if the caller has not."""
    if os.environ.get("RESOLVE_SCRIPT_LIB"):
        return
    if sys.platform == "win32" and _WIN_LIB.is_file():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(_WIN_LIB)
    elif sys.platform == "darwin" and _MAC_LIB.is_file():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(_MAC_LIB)
    elif _LINUX_LIB.is_file():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(_LINUX_LIB)
    elif _LINUX_LIB_ALT.is_file():
        os.environ["RESOLVE_SCRIPT_LIB"] = str(_LINUX_LIB_ALT)


def import_resolve_script() -> Any:
    """Import DaVinciResolveScript, adding the official Modules path if needed."""
    ensure_script_lib_env()
    try:
        import DaVinciResolveScript as dvr  # type: ignore

        return dvr
    except ImportError:
        pass
    for folder in resolve_module_paths():
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    import DaVinciResolveScript as dvr  # type: ignore

    return dvr


# Blackmagic 21.1 feature sheet: "Scripting API: Studio".
# Before 21.1, Workspace > Scripts still ran on free, but external
# scriptapp() already required Studio. 21.1 removed the Python API
# from the free edition entirely.
MIN_STUDIO_NOTE = "21.1+"
FREE_FRIENDLY_HINT = (
    "For a free-editor path, set DIRECTOR_EDITOR=otio and project.save to "
    ".otio, .fcpxml, or .mlt. Free Resolve can import OTIO and FCPXML; "
    "Shotcut/Kdenlive open the MLT XML."
)

SCRIPTING_UNAVAILABLE = (
    "Could not attach to DaVinci Resolve via DaVinciResolveScript / "
    "fusionscript. The live Resolve backend only works on Resolve Studio "
    f"{MIN_STUDIO_NOTE} with Preferences > System > General > External "
    "scripting = Local. The free edition has had no Scripting API since "
    "21.1 (Blackmagic lists it as Studio-only); external scripting already "
    f"needed Studio before that. {FREE_FRIENDLY_HINT} See docs/RESOLVE.md."
)

FREE_EDITION = (
    "Connected to DaVinci Resolve, but this is the free edition — there is "
    "no Scripting API to drive the timeline (Blackmagic 21.1 feature sheet: "
    f"'Scripting API: Studio'). This backend needs Resolve Studio {MIN_STUDIO_NOTE} "
    "and External scripting = Local. "
    f"{FREE_FRIENDLY_HINT}"
)

STUDIO_ONLY_FEATURES = (
    "CreateSubtitlesFromAudio",
    "SmartReframe",
    "SetVoiceIsolationState",
)

STUDIO_HINT = SCRIPTING_UNAVAILABLE


def scripting_unavailable_error(**extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": SCRIPTING_UNAVAILABLE,
        "editor": "resolve",
        "studio_required": True,
        "scripting_available": False,
        "min_studio_version": MIN_STUDIO_NOTE,
        "reason": "scripting_unavailable",
        "hint": FREE_FRIENDLY_HINT,
        "studio_only_features": list(STUDIO_ONLY_FEATURES),
    }
    out.update(extra)
    return out


def free_edition_error(product: str | None = None, version: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": FREE_EDITION,
        "editor": "resolve",
        "studio_required": True,
        "scripting_available": True,
        "studio": False,
        "min_studio_version": MIN_STUDIO_NOTE,
        "reason": "free_edition",
        "product": product or "DaVinci Resolve",
        "version": version,
        "hint": FREE_FRIENDLY_HINT,
        "studio_only_features": list(STUDIO_ONLY_FEATURES),
    }
    out.update(extra)
    return out


def product_is_studio(product: str | None) -> bool:
    return bool(product) and "studio" in product.lower()


def read_product(app: Any) -> str:
    try:
        return str(app.GetProductName() or "DaVinci Resolve")
    except Exception:
        return "DaVinci Resolve"


def read_version(app: Any) -> Any:
    try:
        return app.GetVersionString()
    except Exception:
        try:
            return app.GetVersion()
        except Exception:
            return None


def connect_resolve() -> Any | None:
    """Return the Resolve app object, or None if scripting is unavailable."""
    try:
        dvr = import_resolve_script()
    except ImportError:
        return None
    try:
        return dvr.scriptapp("Resolve")
    except Exception:
        return None


def find_resolve_exe() -> str | None:
    env = os.environ.get("RESOLVE_EXE") or os.environ.get("RESOLVE_PATH")
    if env and os.path.isfile(env):
        return env
    for cand in RESOLVE_EXE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return shutil.which("resolve") or shutil.which("Resolve") or shutil.which("Resolve.exe")


def resolve_running() -> bool:
    names = ("Resolve.exe", "Resolve", "resolve")
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Resolve.exe"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "Resolve.exe" in (proc.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        proc = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=10)
        text = proc.stdout or ""
        return any(n in text for n in names)
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_resolve(*, nogui: bool = False) -> dict[str, Any]:
    exe = find_resolve_exe()
    if not exe:
        return {
            "ok": False,
            "error": "DaVinci Resolve executable not found. Set RESOLVE_EXE.",
        }
    args = [exe]
    if nogui or os.environ.get("RESOLVE_NOGUI", "").strip() in {"1", "true", "yes"}:
        args.append("-nogui")
    try:
        subprocess.Popen(args, close_fds=True)
    except OSError as exc:
        return {"ok": False, "error": f"failed to launch Resolve: {exc}"}
    return {"ok": True, "exe": exe, "args": args, "used_mode": "launched_resolve"}
