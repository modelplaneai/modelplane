# docs/Makefile — regenerate docs/cli.html from the mp Click command tree.
#
# Run from the repo root:
#   make -C docs        # regenerate docs/cli.html
#   make -C docs check  # CI gate: fail if cli.html is out of sync

PYTHON ?= python3
REPO_ROOT := $(realpath ..)

.PHONY: all check clean

all: cli.html

cli.html: build.py $(shell find $(REPO_ROOT)/cli/src/mp -name '*.py')
	@$(PYTHON) -W ignore $(REPO_ROOT)/docs/build.py

check:
	@$(PYTHON) -W ignore $(REPO_ROOT)/docs/build.py >/dev/null
	@if ! git -C $(REPO_ROOT) diff --quiet docs/cli.html; then \
	  echo "ERROR: docs/cli.html is out of sync with the Click tree."; \
	  echo "Run 'make -C docs' and commit the result."; \
	  git -C $(REPO_ROOT) --no-pager diff docs/cli.html | head -40; \
	  exit 1; \
	fi
	@echo "docs/cli.html is in sync with the Click tree."
