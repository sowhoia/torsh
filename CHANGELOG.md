# Changelog

All notable changes to torsh are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [semantic versioning](https://semver.org/).

## [1.0.0]

torsh stops being a Transmission remote and becomes a BitTorrent client.

### Added

- **Embedded engine.** A libtorrent 2.0 session lives inside torsh
  (`torsh/engine.py`): no external daemon, no RPC port or password, no sudo.
  DHT, LSD, µTP, PEX and encryption are on by default.
- **Background daemon** (`torsh daemon`) owning the engine and serving
  newline-delimited JSON-RPC over a private Unix socket in `/tmp/torsh-<uid>`
  (token-guarded loopback TCP where `AF_UNIX` is unavailable). Any command
  starts it on demand; downloads keep running after every frontend exits.
- **Scriptable CLI** in the spirit of `gh`: `add · get · ls · info · files ·
  peers · trackers · pause · resume · rm · speed · limit · seq · prio · queue ·
  move · export · verify · reannounce · stats · doctor · completion`.
  `--json` on every read command, data on stdout and status on stderr, exit
  codes 0/1/2 (and 3 for "daemon not running").
- **Humane selectors** everywhere a torrent is named: id, name substring,
  infohash prefix, or a group (`all`, `active`, `paused`, `done`, `seeding`,
  `downloading`, `errored`).
- **Live TUI** (`torsh` with no arguments): cell-diffed table, fuzzy filter,
  multi-select, details panel (overview / files / peers / trackers), speed
  sparklines, finish/error toasts and a command palette holding every action.
- `torsh queue <selector> up|down|top|bottom` and `shift+↑/↓` in the TUI, to
  reorder torrents waiting behind `limits.active_downloads`.
- `torsh export <selector> [target]` — write a live torrent's metadata back
  out as a `.torrent` file, magnets included.
- `torsh add -` reads one URI per line from stdin.
- **Sequential (streaming) mode** per torrent, and per-file priorities
  including `skip`.
- **Crash-safe resume**: fast-resume blobs carry the info dict, are written
  as metadata arrives, periodically, and on shutdown. Every file torsh
  persists — resume blobs, the id map, session state, the config — goes
  through one durable write path (unique staging file → `fsync` → atomic
  rename), so a power cut can lose the *new* contents but never the old.
- **TOML config** at `~/.config/torsh/config.toml` with dotted-key access
  (`torsh config get/set`), live reload of the running daemon, and
  clamping/coercion so a hand-edited file can never brick torsh.
- `torsh doctor` — engine, config, disk space, state dir, daemon and DHT in
  one pass. It also names an unparseable config (which `load` otherwise
  survives silently) and flags a daemon left running by an upgrade.
- `torsh daemon stop` escalates to `SIGTERM` if the daemon ignores a clean
  shutdown request, and sweeps the socket/pid files a crash left behind.
- Test suite of integration tests over mocks, including a real localhost
  transfer between two libtorrent sessions, the token-guarded TCP transport,
  and the RPC retry rules that decide when a lost reply may be re-sent.

### Security

- The daemon's auth token (loopback TCP transport) is compared in constant
  time and its endpoint file is created private, with no world-readable
  window between write and `chmod`. An unauthenticated peer is disconnected
  after 5 seconds instead of holding a connection slot.
- A `.torrent` URL may not redirect off `http(s)`: urllib follows 3xx into
  `ftp://` by default, which would have sidestepped the scheme check.

### Changed

- **Breaking:** the Transmission integration is gone. Existing
  transmission-daemon setups keep working on their own; torsh no longer
  manages them. `downloads.dir` migrates automatically from the 0.x YAML
  config; nothing else carries over.
- State moved to XDG locations: config in `~/.config/torsh`, state (resume
  data, session state, logs) in `~/.local/state/torsh`, cache in
  `~/.cache/torsh`. All are overridable with `TORSH_*` variables.
- Python 3.10+ is required; the CLI import path deliberately avoids
  libtorrent, asyncio and Textual so `torsh ls` starts cold in ~35 ms.
- The version is declared once, in `torsh/__init__.py`; packaging metadata
  reads it from there. torsh ships `py.typed`.
- A failed `torsh add`/`ls` no longer prints a traceback for an environment
  problem, and piping to `head` exits like `SIGPIPE` instead of trailing an
  "Exception ignored" epilogue.
- A daemon that dies on startup is reported immediately, with its last log
  lines, rather than after the full 15-second startup budget.

[1.0.0]: https://github.com/sowhoia/torsh/releases/tag/v1.0.0
