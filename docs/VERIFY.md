# Verify

One command that proves a change works. Agents and humans run the same
path; CI runs it too.

```bash
# from the repo root, after: cd server && pip install -e ".[dev]"
# ffmpeg / ffprobe on PATH (or FFMPEG_BIN / FFPROBE_BIN)
make verify
```

`make verify` fails fast. The last `VERIFY FAILED: <stage>` line names the
fix. Stages, in order:

| Stage | Command | What it checks |
|---|---|---|
| lint | `make lint` | `scripts/check_boundary.py` (private terms must stay out of this public repo) then `ruff check` on `server/rushcut`, `server/tests`, and `scripts` (rules `E`, `F`, `W`; `E501`/`E701` ignored) |
| typecheck | `make typecheck` | `mypy` on the typed verify scripts (`scripts/check_boundary.py`, `scripts/verify_smoke.py`) |
| unit tests | `make test` | `pytest -q` in `server/` (mocked hosts, synthetic ffmpeg clips; no live VEGAS or Resolve) |
| smoke | `make smoke` | a 0.8 s lavfi fixture rendered through the real `rushcut apply` CLI, then `rushcut check-media` on the file |

The smoke asserts what a caller can see: exit code 0, a non-empty
`out.mp4`, a sibling `.edit.json` sidecar, and duration within 0.15 s of
0.8 s (from both apply JSON and `check-media`). It is not a unit test
importing `apply_timeline`.

Typical runtime is a couple of minutes (pytest plus one tiny encode). If
it takes much longer, something is wrong — do not wait it out.

`make` uses `server/.venv/bin/python` when that venv exists. Override
with `PYTHON=/path/to/python`.

## How to read a failure

```
VERIFY FAILED: lint — fix ruff or the public-boundary check (make lint)
VERIFY FAILED: typecheck — fix the type errors named above (make typecheck)
VERIFY FAILED: unit tests — fix the failing pytest (make test)
VERIFY FAILED: smoke — fixture render failed (output files, duration, or exit code)
VERIFY FAILED: smoke — duration: check-media reported 0.000s, expected 0.800s ± 0.150s
```

Re-run only the named stage (`make lint`, `make typecheck`, `make test`,
`make smoke`) while you fix it. Then run `make verify` once more before
you call the change done.

A runner flake (apt lock, port in use, cancelled or timed-out job) is not
a product failure. Re-run the job; do not "fix" it in code.

## How to add a done-check

A done-check is a command with an expected result, written before the
fix. Prefer something a caller can see: an output file, an exit code, a
duration, an HTTP body. Do not assert internals.

1. Write the command and the expected result in the PR or issue.
2. If it is cheap and always-on, add it as a new isolated file under
   `scripts/` or `server/tests/` and call it from the matching stage.
   Keep `make verify` under a few minutes.
3. If it is slow, optional, or needs live VEGAS/Resolve, leave it out of
   `make verify` and document it as an extra target or a manual check.
4. Prove the new check fails on the old mistake and passes on the fix.
5. Expand typecheck by adding a module path to the `mypy` invocation in
   the Makefile (and only then). The package is not fully typed yet.

CI job name stays `test`. The workflow runs `make verify` after installing
`server/.[dev]` and ffmpeg.
