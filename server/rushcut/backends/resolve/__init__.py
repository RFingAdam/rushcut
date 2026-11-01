"""DaVinci Resolve backend (Studio + external scripting)."""
from .backend import ResolveBackend
from .connect import connect_resolve, resolve_module_paths

__all__ = ["ResolveBackend", "connect_resolve", "resolve_module_paths"]
