"""Locate VEGAS Pro 22 and start it so the in-process host comes up.

VEGAS Pro 22 has no documented GUI-less / headless render mode. Official
command-line switches (Using Scripting help):

  Vegas220.exe /NOLOGO
  Vegas220.exe /OPEN "project.veg"
  Vegas220.exe /RUNSCRIPT "script.cs"   (also /SCRIPT)
  Vegas220.exe /SCRIPTARGS "name=value" /SCRIPT "script.cs"
  Vegas220.exe /CMDMODULE "extension.dll"

Application Extensions in the real Documents\\Vegas Application Extensions
folder (Windows Known Folder MyDocuments — follows OneDrive redirection)
are loaded at startup and stay loaded (Application Extensions FAQ). That
is the preferred auto-start path. /RUNSCRIPT is the Script Menu fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .windows_paths import vegas_application_extensions_dir


# Newest first. Boris FX VEGAS Pro 2027 installs under C:\Program Files\BorisFX.
DEFAULT_EXE_CANDIDATES = [
    r"C:\Program Files\BorisFX\Vegas Pro 2027\vegas2027.exe",
    r"C:\Program Files\VEGAS\VEGAS Pro 22.0\Vegas220.exe",
    r"C:\Program Files\VEGAS\VEGAS Pro 22\Vegas220.exe",
    r"C:\Program Files (x86)\VEGAS\VEGAS Pro 22.0\Vegas220.exe",
]

VEGAS_IMAGE_NAMES = ("vegas2027.exe", "Vegas220.exe")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def host_script_path() -> Path:
    override = os.environ.get("VEGAS_HOST_SCRIPT")
    if override:
        return Path(override)
    return repo_root() / "host" / "VegasDirectorHost.cs"


def extension_dll_candidates() -> list[Path]:
    """DLL locations VEGAS Pro 22 actually scans (Application Extensions FAQ).

    Documents is the Known Folder path, not ``%USERPROFILE%\\Documents``.
    """
    local = os.environ.get("LOCALAPPDATA") or ""
    roaming = os.environ.get("APPDATA") or ""
    programdata = os.environ.get("PROGRAMDATA") or ""
    docs_dll = vegas_application_extensions_dir() / "VegasDirector.dll"
    # Keep Path.home()/Documents as a last-resort extra in case Known Folder
    # resolution failed on a weird install — skip it when it is the same path.
    naive_docs_dll = Path.home() / "Documents" / "Vegas Application Extensions" / "VegasDirector.dll"
    candidates = [
        docs_dll,
        # Per-version folders written by host\build.ps1 (one build per ScriptPortal.Vegas version).
        Path(local) / "VEGAS Pro" / "2027.0" / "Application Extensions" / "VegasDirector.dll",
        Path(roaming) / "VEGAS Pro" / "2027.0" / "Application Extensions" / "VegasDirector.dll",
        Path(local) / "VEGAS Pro" / "22.0" / "Application Extensions" / "VegasDirector.dll",
        Path(roaming) / "VEGAS Pro" / "22.0" / "Application Extensions" / "VegasDirector.dll",
        Path(programdata) / "Vegas Pro" / "22.0" / "Application Extensions" / "VegasDirector.dll",
        Path(local) / "Vegas Pro" / "Application Extensions" / "VegasDirector.dll",
        Path(roaming) / "Vegas Pro" / "Application Extensions" / "VegasDirector.dll",
        Path(programdata) / "Vegas Pro" / "Application Extensions" / "VegasDirector.dll",
        repo_root() / "host" / "bin" / "VegasDirector.dll",
    ]
    if naive_docs_dll != docs_dll:
        candidates.append(naive_docs_dll)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in candidates:
        key = str(p)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def find_vegas_exe() -> str | None:
    env = os.environ.get("VEGAS_EXE") or os.environ.get("VEGAS220_EXE")
    if env and os.path.isfile(env):
        return env
    for cand in DEFAULT_EXE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    for name in VEGAS_IMAGE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def extension_installed() -> bool:
    return any(p.is_file() for p in extension_dll_candidates() if p.as_posix() != ".")


def vegas_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        for image in VEGAS_IMAGE_NAMES:
            proc = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if image.lower() in (proc.stdout or "").lower():
                return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_vegas(
    *,
    project_path: str | None = None,
    run_host_script: bool = True,
    extra_args: list[str] | None = None,
) -> dict:
    """Start VEGAS so the host can bind 127.0.0.1:8752.

    If the Application Extension DLL is installed, we only /OPEN the project
    (the module starts the listener in InitializeModule). Otherwise we
    /RUNSCRIPT the compile-on-demand host.
    """
    if sys.platform != "win32":
        return {
            "ok": False,
            "error": "VEGAS Pro 22 only runs on Windows. Analysis tools still work here.",
        }
    exe = find_vegas_exe()
    if not exe:
        return {
            "ok": False,
            "error": (
                "VEGAS exe not found. Set VEGAS_EXE to vegas2027.exe or Vegas220.exe "
                "(default installs: C:\\Program Files\\BorisFX\\Vegas Pro 2027\\, C:\\Program Files\\VEGAS\\VEGAS Pro 22.0\\)"
            ),
        }
    args = [exe, "/NOLOGO"]
    if project_path:
        args.extend(["/OPEN", project_path])
    used_extension = extension_installed()
    if used_extension:
        mode = "launched_extension"
    elif run_host_script:
        script = host_script_path()
        if not script.is_file():
            return {"ok": False, "error": f"host script missing: {script}"}
        args.extend(["/RUNSCRIPT", str(script)])
        mode = "launched_runscript"
    else:
        mode = "launched_vegas_only"
    if extra_args:
        args.extend(extra_args)
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "error": f"failed to launch VEGAS: {exc}"}
    return {
        "ok": True,
        "used_mode": mode,
        "exe": exe,
        "extension_installed": used_extension,
        "args": args[1:],
    }


def run_oneshot_script(script_path: str, *, project_path: str | None = None, script_args: str | None = None) -> dict:
    """Vegas220.exe /RUNSCRIPT generated.cs — still opens the VEGAS UI."""
    if sys.platform != "win32":
        return {"ok": False, "error": "one-shot /RUNSCRIPT requires Windows + VEGAS Pro 22"}
    exe = find_vegas_exe()
    if not exe:
        return {"ok": False, "error": "VEGAS exe not found (set VEGAS_EXE)"}
    if not os.path.isfile(script_path):
        return {"ok": False, "error": f"script not found: {script_path}"}
    args = [exe, "/NOLOGO"]
    if project_path:
        args.extend(["/OPEN", project_path])
    if script_args:
        args.extend(["/SCRIPTARGS", script_args])
    args.extend(["/RUNSCRIPT", script_path])
    try:
        proc = subprocess.Popen(args)
    except OSError as exc:
        return {"ok": False, "error": f"failed to launch VEGAS: {exc}"}
    return {
        "ok": True,
        "used_mode": "oneshot_runscript",
        "pid": proc.pid,
        "exe": exe,
        "script_path": script_path,
    }
