---
name: verify
description: Run the repo's one-command proof that a change works. Use when finishing a change, before opening a PR, or when a check failed and you need to read the stage error.
---

# Verify

Run this yourself. Do not hand the checking to Adam.

```bash
make verify
```

From the repo root, after `pip install -e ".[dev]"` in `server/` and with
`ffmpeg` on PATH. Uses `server/.venv` when present. Full notes:
[docs/VERIFY.md](../../../docs/VERIFY.md).

## Stages (fail-fast)

1. **lint** — `scripts/check_boundary.py` then `ruff check` (`E`,`F`,`W`).
   Fix: the ruff or boundary line named in the output.
2. **typecheck** — `mypy` on `scripts/check_boundary.py` and
   `scripts/verify_smoke.py`. Fix: the type error named above.
3. **unit tests** — `pytest -q` in `server/`. Fix: the failing test.
4. **smoke** — tiny lavfi clip through the real `rushcut apply` CLI, then
   `rushcut check-media`. Asserts output files, duration (~0.8 s), exit 0.

A failure prints `VERIFY FAILED: <stage> — <fix>`. Re-run that stage,
then `make verify` once more.

## Done-checks

Before coding, write a command with an expected result. If you cannot,
the work is not scoped. A new always-on check belongs in an isolated
file (`scripts/` or `server/tests/`) wired into the matching stage. Keep
the whole run to a few minutes. Prove it fails on the old mistake.

Mark each done-check `VERIFIED`, `NOT VERIFIED`, or `INCONCLUSIVE` with
the command and output. Inconclusive is not a pass. Never accept a
summary (yours or a subagent's) as proof — run `make verify` and, for
behavior changes, the real CLI or repro.
