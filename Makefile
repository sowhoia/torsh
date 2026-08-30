APP?=torsh
VERSION?=$(shell python3 -c "import torsh;print(torsh.__version__)" 2>/dev/null || echo 1.0.0)
DIST_DIR:=dist
PY:=.venv/bin/python

.PHONY: dev test cov fmt lint check run dist brew clean

## Development

dev:                            ## create a venv and install torsh editable + dev deps
	uv venv .venv --python 3.12
	uv pip install --python $(PY) -e '.[dev]'

test:                           ## run the test suite
	$(PY) -m pytest

cov:                            ## run the suite with a coverage report
	$(PY) -m pytest --cov --cov-report=term-missing

fmt:                            ## reformat the tree (ruff format)
	$(PY) -m ruff format torsh tests

lint:                           ## static checks (ruff lint + format + mypy)
	$(PY) -m ruff check torsh tests
	$(PY) -m ruff format --check torsh tests
	$(PY) -m mypy

check: lint                     ## lint + byte-compile sanity check
	$(PY) -m compileall -q torsh

run:                            ## launch the TUI from the venv
	$(PY) -m torsh

## Packaging

dist:                           ## build sdist + wheel into dist/
	python3 -m pip install --upgrade build
	python3 -m build

brew-sha:                       ## print the published sdist sha256, for packaging/homebrew/torsh.rb
	curl -sL https://pypi.org/pypi/torsh/json | \
		$(PY) -c "import json,sys; d=json.load(sys.stdin); print(next(u['digests']['sha256'] for u in d['urls'] if u['packagetype']=='sdist' and u['filename']==f'torsh-{d[\"info\"][\"version\"]}.tar.gz'))"

brew:
	@echo "Update url/sha256 in packaging/homebrew/torsh.rb before running (make brew-sha)."
	brew install --build-from-source ./packaging/homebrew/torsh.rb

clean:
	rm -rf $(DIST_DIR) build *.egg-info
