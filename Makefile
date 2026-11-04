# One command that proves a change works. See docs/VERIFY.md.
# From the repo root: make verify
# Uses server/.venv when present; override with PYTHON=/path/to/python.

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

ifeq ($(origin PYTHON), undefined)
  ifneq ($(wildcard $(ROOT)/server/.venv/bin/python),)
    PYTHON := $(ROOT)/server/.venv/bin/python
  else
    PYTHON := python3
  endif
endif

.PHONY: help verify lint typecheck test smoke

help:
	@echo "make verify     lint, typecheck, unit tests, then a CLI smoke render"
	@echo "make lint       public boundary check + ruff"
	@echo "make typecheck  mypy on the typed verify scripts"
	@echo "make test       pytest -q in server/"
	@echo "make smoke      tiny fixture through rushcut apply (files, duration, exit)"

verify:
	@$(MAKE) --no-print-directory lint || { \
	  echo "VERIFY FAILED: lint — fix ruff or the public-boundary check (make lint)" >&2; \
	  exit 1; \
	}
	@$(MAKE) --no-print-directory typecheck || { \
	  echo "VERIFY FAILED: typecheck — fix the type errors named above (make typecheck)" >&2; \
	  exit 1; \
	}
	@$(MAKE) --no-print-directory test || { \
	  echo "VERIFY FAILED: unit tests — fix the failing pytest (make test)" >&2; \
	  exit 1; \
	}
	@$(MAKE) --no-print-directory smoke || { \
	  echo "VERIFY FAILED: smoke — fixture render failed (output files, duration, or exit code). See docs/VERIFY.md" >&2; \
	  exit 1; \
	}
	@echo "VERIFY PASSED"

lint:
	@echo "==> lint"
	$(PYTHON) $(ROOT)/scripts/check_boundary.py
	$(PYTHON) -m ruff check --config $(ROOT)/server/pyproject.toml \
	  $(ROOT)/server/rushcut $(ROOT)/server/tests $(ROOT)/scripts

typecheck:
	@echo "==> typecheck"
	$(PYTHON) -m mypy --config-file=$(ROOT)/server/pyproject.toml \
	  $(ROOT)/scripts/check_boundary.py $(ROOT)/scripts/verify_smoke.py

test:
	@echo "==> unit tests"
	cd $(ROOT)/server && $(PYTHON) -m pytest -q

smoke:
	@echo "==> smoke"
	$(PYTHON) $(ROOT)/scripts/verify_smoke.py
