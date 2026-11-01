#!/usr/bin/env python3
"""Fail if private product code or terms land in this public repo.

Rushcut is the generic editing bridge. Clip selection, ranking and channel
specifics live elsewhere. Terms are stored as SHA-256 hashes so this file does
not publish the words it guards against.

Add a term:  python scripts/check_boundary.py --hash "word"
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED_FILENAMES = {"highlights.py", "propose.py", "shorts.py"}

BLOCKED_TERM_HASHES = {
    "5f0da3ea850a2438c9dd40af234c7ce5858c077228872bbfeb6f8b06dc9a293e",
    "a92b7a6a88cf096bc7264dc1f334f64328084b6f3f2ad40b6fb6bd133da8fb1e",
    "ac6fd1e618ae39b4148ed534ecbf735d5a1185512b8923ede325a2171c0ac388",
    "bdd65dcd9f231498fa89dad79b0e7263536b90125bd7f2a4900c5f7e23552a10",
    "c810fb4ec91b45400213adbaae0464ac06bdac495740c54c5c30506d2d1b36ba",
    "d02425c564fbcc3a24fa78ccc2ea9b4d81e527b0b9a45c97a6f0f5be720628e1",
    "e520cdec7d2383a4aaffca8958b8fd8e2f4444f477b533d404de92db1cdf5399",
}

TOKEN = re.compile(r"[a-z0-9_]+")
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gif", ".webp", ".dll", ".zip", ".ttf"}


def term_hash(word: str) -> str:
    return hashlib.sha256(word.strip().lower().encode("utf-8")).hexdigest()


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def scan(paths: list[str]) -> list[str]:
    problems: list[str] = []
    for rel in paths:
        if Path(rel).name in BLOCKED_FILENAMES:
            problems.append(f"{rel}: filename belongs in the private repo")
        if Path(rel).suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in TOKEN.findall(line):
                if term_hash(token) in BLOCKED_TERM_HASHES:
                    problems.append(f"{rel}:{lineno}: blocked term")
                    break
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash", metavar="WORD", help="print the hash for a new term")
    args = parser.parse_args()
    if args.hash:
        print(term_hash(args.hash))
        return 0
    problems = scan(tracked_files())
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} boundary problem(s)", file=sys.stderr)
        return 1
    print("boundary check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
