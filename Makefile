# switch-coldstart
#   make check       byte-compile, unit + scenario tests, shellcheck (if installed)
#   make test        unit + scenario tests only
#   make sysmodule   clone sys-botbase at the pinned revision and build it with devkitPro
#   make clean       remove caches and test leftovers (not runs/ or refs/)

PYTHON      ?= python3
SHELL_FILES := $(wildcard bin/coldstart scripts/*.sh)
PY_DIRS     := $(wildcard coldstart sim tests)
PY_SCRIPTS  := $(wildcard sim/bin/twib sim/bin/twibd)

.PHONY: check compile test shellcheck sysmodule clean

check: compile test shellcheck
	@echo "make check: OK"

compile:
	$(PYTHON) -m compileall -q $(PY_DIRS)
	@for f in $(PY_SCRIPTS); do $(PYTHON) -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$$f" || exit 1; done

test:
	cd tests && PYTHONPATH=$(CURDIR) timeout -s KILL 600 $(PYTHON) -m unittest discover -t . -s . -p 'test_*.py'

shellcheck:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck $(SHELL_FILES) && echo "shellcheck: clean"; \
	else \
		echo "shellcheck: not installed, skipped"; \
	fi

sysmodule:
	scripts/build-sysmodule.sh

clean:
	find . -name '__pycache__' -prune -exec rm -rf {} +
	rm -rf sysmodule/out
