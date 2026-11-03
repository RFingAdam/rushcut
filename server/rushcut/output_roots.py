"""Refuse writes outside allowed output roots (env + edit_profile).

No roots configured → writes are allowed (backward compatible). Windows
drive / UNC / ``..`` semantics are applied even when the process is not
running on Windows, so a dest of ``C:\\review`` is refused when ``C:``
is not in the allowed list.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("rushcut")

ENV_OUTPUT_ROOTS = "RUSHCUT_OUTPUT_ROOTS"
OUTSIDE_ROOTS = "output outside allowed roots"

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")
_UNC_RE = re.compile(r"^\\\\[^\\/]+\\[^\\/]+")


def _split_roots(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"[;]+", raw):
        text = chunk.strip().strip("\"'")
        if text:
            parts.append(text)
    return parts


def _as_profile(profile: Any) -> Any:
    if profile is None:
        return None
    if hasattr(profile, "output_roots"):
        return profile
    if isinstance(profile, (str, os.PathLike)):
        from .profile import load_profile

        return load_profile(profile)
    if isinstance(profile, dict):
        from .profile import profile_from_data

        return profile_from_data(profile)
    return None


def effective_output_roots(profile: Any = None, env: dict[str, str] | None = None) -> list[str]:
    """Merge ``RUSHCUT_OUTPUT_ROOTS`` and ``profile.output_roots`` (env first)."""
    environ = os.environ if env is None else env
    roots: list[str] = []
    seen: set[str] = set()
    for item in _split_roots(str(environ.get(ENV_OUTPUT_ROOTS) or "")):
        key = _norm_key(item)
        if key not in seen:
            seen.add(key)
            roots.append(item)
    obj = _as_profile(profile)
    extra = list(getattr(obj, "output_roots", None) or []) if obj is not None else []
    for item in extra:
        text = str(item).strip()
        if not text:
            continue
        key = _norm_key(text)
        if key not in seen:
            seen.add(key)
            roots.append(text)
    return roots


def _looks_windows(path: str) -> bool:
    return bool(_DRIVE_RE.match(path) or path.startswith("\\\\") or "\\" in path)


def _norm_key(path: str) -> str:
    return _normalize_path(path).casefold()


def _normalize_path(path: str) -> str:
    text = str(path).strip().strip("\"'")
    if not text:
        return ""
    windows = _looks_windows(text)
    if windows:
        text = text.replace("/", "\\")
        unc = _UNC_RE.match(text)
        if unc:
            prefix = unc.group(0)
            rest = text[len(prefix) :]
            parts = [p for p in rest.split("\\") if p not in ("", ".")]
            stack: list[str] = []
            for part in parts:
                if part == "..":
                    if stack:
                        stack.pop()
                    continue
                stack.append(part)
            return prefix + (("\\" + "\\".join(stack)) if stack else "")
        drive_m = _DRIVE_RE.match(text)
        if drive_m:
            drive = drive_m.group(1).upper()
            rest = text[2:]
            parts = [p for p in rest.replace("/", "\\").split("\\") if p not in ("", ".")]
            stack = []
            for part in parts:
                if part == "..":
                    if stack:
                        stack.pop()
                    continue
                stack.append(part)
            return drive + ":\\" + "\\".join(stack)
        parts = [p for p in text.split("\\") if p not in ("", ".")]
        stack = []
        for part in parts:
            if part == "..":
                if stack:
                    stack.pop()
                continue
            stack.append(part)
        return "\\".join(stack)
    expanded = os.path.expanduser(text)
    try:
        resolved = os.path.realpath(expanded) if os.path.exists(expanded) else os.path.abspath(expanded)
    except OSError:
        resolved = os.path.abspath(expanded)
    parts = []
    for part in resolved.split(os.sep):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if resolved.startswith(os.sep):
        return os.sep + os.sep.join(parts) if parts else os.sep
    return os.sep.join(parts)


def path_allowed(path: str, roots: list[str] | None) -> bool:
    """True when ``path`` is under one of ``roots``, or when no roots are set."""
    if not roots:
        return True
    dest = _normalize_path(path)
    if not dest:
        return False
    dest_key = dest.casefold()
    for root in roots:
        base = _normalize_path(root)
        if not base:
            continue
        base_key = base.casefold()
        if dest_key == base_key:
            return True
        sep = "\\" if _looks_windows(base) or _looks_windows(dest) else os.sep
        prefix = base_key.rstrip("\\/") + (sep if not base_key.endswith(("\\", "/")) else "")
        # Drive-only root ("D:") allows anything on that drive.
        if _DRIVE_RE.match(root.strip()) and len(base) <= 3:
            dest_drive = dest[:2].casefold() if len(dest) >= 2 and dest[1] == ":" else ""
            if dest_drive == base_key[:2]:
                return True
        if dest_key.startswith(prefix):
            return True
        if dest_key.startswith(base_key.rstrip("\\/") + "\\") or dest_key.startswith(
            base_key.rstrip("\\/") + "/"
        ):
            return True
    return False


def refuse_output_path(
    path: str,
    *,
    profile: Any = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return an error dict if ``path`` is outside allowed roots, else None."""
    roots = effective_output_roots(profile, env=env)
    if not roots:
        log.warning("RUSHCUT_OUTPUT_ROOTS / output_roots unset; writes are unrestricted")
        return None
    if path_allowed(path, roots):
        return None
    return {"ok": False, "error": OUTSIDE_ROOTS, "path": str(path), "output_roots": roots}
