APP?=torsh
VERSION?=$(shell python3 -c "import torsh;print(torsh.__version__)" 2>/dev/null || echo 0.1.0)
DIST_DIR:=dist

.PHONY: dist sdist wheel deb rpm brew clean

dist: sdist wheel

sdist wheel:
	python3 -m pip install --upgrade build
	python3 -m build

deb:
	python3 -m pip install --upgrade build fpm
	python3 -m build
	fpm -s python -t deb --python-bin python3 .

rpm:
	python3 -m pip install --upgrade build fpm
	python3 -m build
	fpm -s python -t rpm --python-bin python3 .

brew:
	@echo "Update url/sha256 in packaging/homebrew/torsh.rb before running."
	brew install --build-from-source ./packaging/homebrew/torsh.rb

clean:
	rm -rf $(DIST_DIR) build *.egg-info

