# Packaging

## Release (automated)

Releases are cut by pushing a `v*` tag — the `release` workflow then builds the
sdist/wheel, publishes to PyPI via **Trusted Publishing (OIDC)**, and attaches
`.deb`/`.rpm` artifacts to the GitHub Release.

```
git tag -a v0.2.0 main -m "torsh 0.2.0"
git push origin v0.2.0
```

One-time PyPI setup (no API token needed afterwards):
[pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
— add a trusted publisher for repo `sowhoia/torsh`, workflow `release.yml`,
environment `release`.

## PyPI (manual fallback)
```
python3 -m pip install --upgrade build twine
python3 -m build
twine upload dist/*
```

## pipx (from local sdist/wheel)
```
pipx install dist/torsh-*.whl
```

## deb / rpm (requires fpm)
```
make deb   # produces torsh_VERSION_amd64.deb
make rpm   # produces torsh-VERSION.x86_64.rpm
```

## Homebrew (template)
- Edit `packaging/homebrew/torsh.rb` and set `url`/`sha256` to the published tarball (PyPI sdist).
- Then: `make brew`

Generated artifacts use python3 entrypoint `torsh`.

