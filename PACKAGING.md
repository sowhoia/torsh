# Packaging

## Release (automated)

Releases are cut by pushing a `v*` tag — the `release` workflow then builds
the sdist/wheel, publishes to PyPI via **Trusted Publishing (OIDC)**, and
attaches both files to the GitHub Release.

```
git tag -a v1.0.0 main -m "torsh 1.0.0"
git push origin v1.0.0
```

One-time PyPI setup (no API token needed afterwards):
[pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
— add a trusted publisher for repo `sowhoia/torsh`, workflow `release.yml`,
environment `release`.

Before tagging: bump `__version__` in `torsh/__init__.py` (the single source
of truth — `pyproject.toml` reads it) and the `version` line in
`packaging/homebrew/torsh.rb`, then add the release's section to
`CHANGELOG.md`.

Nothing here depends on remembering: `tests/test_packaging.py` fails if the
formula falls behind `torsh.__version__` or if the `requirements*.txt`
mirrors drift from `pyproject.toml`, and the release workflow refuses to
build a tag whose name disagrees with the code.

## PyPI (manual fallback)

```
python3 -m pip install --upgrade build twine
python3 -m build
twine upload dist/*
```

## End users

`pipx install torsh` (or `pip install torsh`) is the supported install on
Linux and macOS — libtorrent ships prebuilt wheels, so no compiler or system
packages are needed. Native deb/rpm packages are deliberately not produced:
they would have to declare distro Python packages that don't track PyPI
(libtorrent bindings above all), producing installs that break on upgrade.

## Homebrew (template)

- Edit `packaging/homebrew/torsh.rb`: set `sha256` to the published PyPI
  sdist hash (command in the formula's comment).
- Test locally with `make brew`, then push the formula to a tap
  (`brew tap sowhoia/torsh && brew install torsh`).
