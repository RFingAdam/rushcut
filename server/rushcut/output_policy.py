"""Output-root and work-root write policy.

Every tool that takes ``output_path`` / ``output_dir`` (and every scratch
dir) must resolve the real path and refuse writes outside the configured
roots. Roots come from ``RUSHCUT_OUTPUT_ROOTS`` / ``RUSHCUT_WORK_ROOTS``
and from ``output_roots`` / ``work_roots`` on ``edit_profile.yaml``.

With no roots configured, behaviour is unchanged (writes unrestricted)
and a one-line warning is logged once.

Windows path semantics (drive letters, ``..``, UNC, case-folding) are
applied even when the process is running on POSIX, so policy tests and
cross-OS agents see the same answers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("rushcut")

OUTSIDE_ROOTS = "output outside allowed roots"

# Documented Windows example default. Configurable via env / profile;
# not the only root the library will accept.
EXAMPLE_WINDOWS_OUTPUT_ROOT = (
    r"D:\Adam\Videos\ShadowPlay Videos\Microsoft Flight Simulator\OBS\_EDITS"
)

_ENV_OUTPUT = "RUSHCUT_OUTPUT_ROOTS"
_ENV_WORK = "RUSHCUT_WORK_ROOTS"
_ENV_PROFILE = "RUSHCUT_PROFILE"

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]?")
_UNC = re.compile(r"^\\\\[^\\]+\\[^\\]+")
_warned_unrestricted = False
_active_profile: Any | None = None


@dataclass(frozen=True)
class OutputPolicy:
    output_roots: tuple[str, ...] = ()
    work_roots: tuple[str, ...] = ()
    source: str = "none"

    @property
    def restricted(self) -> bool:
        return bool(self.output_roots)

    @property
    def effective_work_roots(self) -> tuple[str, ...]:
        return self.work_roots or self.output_roots


def reset_policy_state() -> None:
    """Test helper: drop the one-shot warning and any pinned profile."""
    global _warned_unrestricted, _active_profile
    _warned_unrestricted = False
    _active_profile = None


def set_active_profile(profile: Any | None) -> None:
    """Pin a loaded EditProfile so tools see its roots without re-reading env."""
    global _active_profile
    _active_profile = profile


def _split_root_spec(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{_ENV_OUTPUT}: JSON value must be a list of paths")
        return [str(item).strip() for item in data if str(item).strip()]
    # Never split on ':' — that is the drive-letter separator.
    parts = re.split(r"[;\n]+", text)
    return [p.strip().strip('"') for p in parts if p.strip()]


def _is_windows_style(path: str) -> bool:
    compact = path.replace("/", "\\")
    return bool(_WIN_DRIVE.match(compact) or compact.startswith("\\\\"))


def _win_norm(path: str) -> str:
    p = path.replace("/", "\\")
    if p.startswith("\\\\"):
        tokens = [t for t in p.split("\\") if t not in ("", ".")]
        stack: list[str] = []
        for token in tokens:
            if token == "..":
                if len(stack) > 1:
                    stack.pop()
            else:
                stack.append(token)
        return "\\\\" + "\\".join(stack)
    if _WIN_DRIVE.match(p):
        drive = p[0].upper() + ":"
        rest = p[2:]
        rest = rest.removeprefix("\\")
        tokens = [t for t in rest.split("\\") if t and t != "."]
        stack = []
        for token in tokens:
            if token == "..":
                if stack:
                    stack.pop()
            else:
                stack.append(token)
        return drive + "\\" + "\\".join(stack) if stack else drive + "\\"
    tokens = [t for t in p.split("\\") if t and t != "."]
    stack = []
    for token in tokens:
        if token == "..":
            if stack:
                stack.pop()
        else:
            stack.append(token)
    return "\\".join(stack)


def resolve_policy_path(path: str) -> str:
    """Lexical + realpath resolution used by the write policy."""
    if not path:
        return ""
    raw = os.path.expandvars(os.path.expanduser(str(path)))
    if _is_windows_style(raw):
        return _win_norm(raw)
    abs_path = os.path.abspath(raw)
    parent = abs_path
    # Walk up to the first existing ancestor so junctions/symlinks that
    # already exist are resolved, while a not-yet-created leaf stays put.
    while parent and not os.path.lexists(parent):
        nxt = os.path.dirname(parent)
        if nxt == parent:
            break
        parent = nxt
    if parent and os.path.lexists(parent):
        real_parent = os.path.realpath(parent)
        tail = os.path.relpath(abs_path, parent)
        if tail in (".", ""):
            return os.path.normpath(real_parent)
        return os.path.normpath(os.path.join(real_parent, tail))
    return os.path.normpath(abs_path)


def _casefold_win(path: str) -> str:
    return path.replace("/", "\\").casefold()


def is_under_root(path: str, root: str) -> bool:
    resolved = resolve_policy_path(path)
    resolved_root = resolve_policy_path(root)
    if not resolved or not resolved_root:
        return False
    if _is_windows_style(path) or _is_windows_style(root) or _is_windows_style(resolved) or _is_windows_style(resolved_root):
        p = _casefold_win(resolved).rstrip("\\")
        r = _casefold_win(resolved_root).rstrip("\\")
        if len(r) == 2 and r[1] == ":":
            return p == r or p.startswith(r + "\\")
        return p == r or p.startswith(r + "\\")
    p = resolved.rstrip("/")
    r = resolved_root.rstrip("/")
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False


def _profile_roots(profile: Any | None, attr: str) -> list[str]:
    if profile is None:
        return []
    values = getattr(profile, attr, None)
    if not values:
        extra = getattr(profile, "model_extra", None) or {}
        if isinstance(profile, dict):
            values = profile.get(attr)
        elif extra:
            values = extra.get(attr)
        else:
            dumped = profile.model_dump() if hasattr(profile, "model_dump") else {}
            values = dumped.get(attr)
    if not values:
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def current_policy(*, profile: Any | None = None) -> OutputPolicy:
    """Effective roots: env wins, then the explicit/active/RUSHCUT_PROFILE profile."""
    env_out = _split_root_spec(os.environ.get(_ENV_OUTPUT, ""))
    env_work = _split_root_spec(os.environ.get(_ENV_WORK, ""))
    loaded = profile if profile is not None else _active_profile
    if loaded is None:
        path = os.environ.get(_ENV_PROFILE, "").strip()
        if path:
            from .profile import load_profile

            loaded = load_profile(path)
    file_out = _profile_roots(loaded, "output_roots")
    file_work = _profile_roots(loaded, "work_roots")
    output = tuple(env_out or file_out)
    work = tuple(env_work or file_work or output)
    if env_out:
        source = "env"
    elif file_out:
        source = "profile"
    else:
        source = "none"
    return OutputPolicy(output_roots=output, work_roots=work, source=source)


def _warn_unrestricted() -> None:
    global _warned_unrestricted
    if _warned_unrestricted:
        return
    _warned_unrestricted = True
    log.warning("No output_roots configured; writes are unrestricted")


def check_output_path(
    path: str | None, *, kind: str = "output", profile: Any | None = None
) -> dict[str, Any] | None:
    """Return an ``{ok:false}`` dict when *path* is outside allowed roots.

    ``None`` means the write may proceed. Unrestricted policy also returns
    ``None`` (after the one-line warning). Pass ``profile`` to evaluate a
    specific edit profile without pinning it as the process-wide active one.
    """
    if not path:
        return None
    policy = current_policy(profile=profile)
    roots = policy.work_roots if kind == "work" else policy.output_roots
    if not roots:
        _warn_unrestricted()
        return None
    if any(is_under_root(path, root) for root in roots):
        return None
    return {"ok": False, "error": OUTSIDE_ROOTS, "path": str(path), "roots": list(roots)}


def require_output_path(
    path: str | None, *, kind: str = "output", profile: Any | None = None
) -> dict[str, Any] | None:
    """Like :func:`check_output_path` but also rejects empty paths."""
    if not path:
        return {"ok": False, "error": "output_path is required"}
    return check_output_path(path, kind=kind, profile=profile)


def work_parent() -> str | None:
    """Directory scratch files should land in, or ``None`` for the system temp."""
    policy = current_policy()
    roots = policy.effective_work_roots
    if not roots:
        _warn_unrestricted()
        return None
    root = roots[0]
    if _is_windows_style(root) and os.name != "nt":
        # Lexical Windows root on POSIX: callers that need a real directory
        # should pass a POSIX work root in tests. Still report the policy root.
        return None
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)


def make_work_dir(prefix: str = "rushcut-") -> str:
    """``mkdtemp`` under the first work/output root, else the system temp."""
    parent = work_parent()
    if parent is None:
        return tempfile.mkdtemp(prefix=prefix)
    return tempfile.mkdtemp(prefix=prefix, dir=parent)


def work_named_tempfile(suffix: str, mode: str = "w", encoding: str = "utf-8"):
    """``NamedTemporaryFile`` that honors ``work_roots``."""
    parent = work_parent()
    kwargs: dict[str, Any] = {
        "mode": mode,
        "suffix": suffix,
        "delete": False,
        "encoding": encoding,
    }
    if parent:
        kwargs["dir"] = parent
    return tempfile.NamedTemporaryFile(**kwargs)


def policy_snapshot(profile: Any | None = None) -> dict[str, Any]:
    policy = current_policy(profile=profile)
    return {
        "output_roots": list(policy.output_roots),
        "work_roots": list(policy.effective_work_roots),
        "output_policy": "restricted" if policy.restricted else "unrestricted",
        "output_roots_source": policy.source,
    }


def scan_stale_partials(roots: Iterable[str] | None = None) -> list[str]:
    """Find leftover ``*.partial.*`` files. Never deletes them."""
    policy = current_policy()
    search = list(roots) if roots is not None else list(policy.effective_work_roots)
    found: list[str] = []
    for root in search:
        if _is_windows_style(root) and os.name != "nt":
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if ".partial." in name or name.endswith(".partial"):
                    found.append(os.path.join(dirpath, name))
    return sorted(found)


def report_stale_partials(roots: Iterable[str] | None = None) -> list[str]:
    found = scan_stale_partials(roots)
    for path in found:
        log.warning("stale partial render left behind: %s", path)
    return found
