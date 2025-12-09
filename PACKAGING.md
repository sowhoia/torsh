# Packaging

## PyPI
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

