"""The published package carries its own copies of these repository files."""

from __future__ import annotations

from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_packaged_copy_matches_repository_root(name: str) -> None:
    assert (SERVER / name).read_bytes() == (SERVER.parent / name).read_bytes()
