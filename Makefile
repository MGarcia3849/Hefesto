PYTHON ?= $(shell command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && echo python3 || echo python)

.PHONY: lint test smoke smoke-live

lint:
	$(PYTHON) tools/dev.py lint

test:
	$(PYTHON) tools/dev.py test

smoke:
	$(PYTHON) tools/dev.py smoke

smoke-live:
	$(PYTHON) tools/dev.py smoke-live
