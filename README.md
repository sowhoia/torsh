# torsh — Transmission TUI

One-command Transmission client (btop-style). Runs `torsh`; it will start Transmission (and can auto-install it) or connect to an existing RPC.

## Install
- pipx (recommended): `pipx install torsh`
- pip: `python3 -m pip install torsh`

Needs Python 3.10+. If `transmission-daemon` is missing, torsh will try to install it via the available package manager (can be disabled).

## Run
```bash
torsh
# optional:
# torsh --host localhost --port 9091 --download-dir ~/Downloads/torrents --no-autostart --no-install-missing
```

## Keys
- `a` add magnet/.torrent (choose download dir)
- `space` pause/resume
- `d` delete (ask about data)
- `g` change download dir
- `s` set speed limits
- `p` set file priorities
- `l` view daemon log (auto-refresh tail)
- `/` filter by name, `]`/`[` adjust refresh rate, `r` manual refresh
- `?` help, `q` quit
- UI remembers filters/sort/refresh interval between sessions.

## Config
Auto-created at `~/.config/torsh/config.yaml`. Env/flags override:
`TORSH_HOST`, `TORSH_PORT`, `TORSH_USER`, `TORSH_PASSWORD`, `TORSH_TIMEOUT`,
`TORSH_DOWNLOAD_DIR`, `TORSH_INSTALL_MISSING`, `TORSH_RESTART_ON_FAIL`, `TORSH_LOG`.

Packaging notes: see `PACKAGING.md` (sdist/wheel, deb/rpm via fpm, brew formula template).

## License
MIT.
