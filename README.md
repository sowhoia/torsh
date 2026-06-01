<div align="center">

# 🌊 Torsh

**A beautiful, modern Transmission client for your terminal.**

One command. Full control. No browser, no clutter — just a fast, keyboard-driven TUI.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-00f0ff.svg?style=flat-square)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/torsh.svg?style=flat-square&color=ff3cac)](https://pypi.org/project/torsh/)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-8f7fff.svg?style=flat-square)](https://textual.textualize.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-2effa8.svg?style=flat-square)](https://opensource.org/licenses/MIT)

</div>

---

## ✨ Features

| | |
|---|---|
| 📊 **Live dashboard** | Sparkline speed graphs, disk usage, real-time session stats |
| 📋 **Smart torrent list** | Per-row progress bars, ratios, ETA, peers — sortable on every column |
| 🎯 **Instant filters** | Filter by name, status (active/paused/error) or progress |
| 📁 **File browser** | Inspect individual files and set per-file priorities |
| 🔗 **Tracker insights** | Live tracker status, seeders and leechers |
| ⚡ **Speed control** | Global *and* per-torrent limits, with handy presets |
| 🔔 **Toast notifications** | Get notified the moment a download finishes |
| ⌨️ **Vim-style keys** | `j`/`k` navigation, single-key actions, discoverable help |
| 💾 **Session memory** | Remembers your filters, sort order and refresh rate |
| 🚀 **Zero setup** | Auto-starts (and can auto-install) `transmission-daemon` for you |

## 📦 Install

```bash
# Recommended — isolated, always on your PATH
pipx install torsh

# …or plain pip
pip install torsh
```

<details>
<summary>From source</summary>

```bash
git clone https://github.com/sowhoia/torsh
cd torsh
pip install -e .
```
</details>

Requires **Python 3.10+**. If `transmission-daemon` isn't installed, Torsh can
install it for you via your system package manager (`apt`, `brew`, `dnf`,
`pacman`, `zypper`, …).

## 🚀 Usage

```bash
torsh
```

That's it. Torsh shows a quick boot sequence, starts the daemon if needed,
connects automatically, and drops you into the dashboard.

### Options

```bash
torsh --host localhost --port 9091     # connect to a specific daemon
torsh --user alice --password secret   # authenticated RPC
torsh --download-dir ~/Downloads/torr  # default download directory
torsh --no-autostart                   # never launch the daemon yourself
torsh --no-install-missing             # never auto-install transmission
torsh --no-banner                      # skip the startup banner
torsh --version                        # print version and exit
```

## ⌨️ Keyboard Shortcuts

| Key | Action | | Key | Action |
|:---:|--------|---|:---:|--------|
| `a` | Add magnet / `.torrent` | | `/` | Filter by name |
| `space` | Pause / resume | | `c` | Cycle status filter |
| `d` | Delete **with** data | | `o` | Cycle progress filter |
| `x` | Delete, **keep** data | | `g` | Move data location |
| `v` | Verify (re-check) | | `r` | Refresh now |
| `s` | Global speed limits | | `?` | Help |
| `t` | Per-torrent speed limits | | `q` | Quit |
| `p` | File priorities | | `[` `]` | Slower / faster refresh |

**Navigation:** `j` / `k` move • `G` jump to bottom • `Tab` switch panes
**Sorting:** click a column header or press `1`–`8`

## ⚙️ Configuration

A config file is auto-created at `~/.config/torsh/config.yaml` and updated as
you go. Anything set there can be overridden by CLI flags or environment
variables.

| Variable | Purpose |
|----------|---------|
| `TORSH_HOST`, `TORSH_PORT` | RPC connection |
| `TORSH_USER`, `TORSH_PASSWORD` | RPC authentication |
| `TORSH_DOWNLOAD_DIR` | Default download directory |
| `TORSH_CONFIG_DIR` | Where config & daemon state live |
| `TORSH_AUTOSTART` | `false` to never start the daemon |
| `TORSH_INSTALL_MISSING` | `false` to never auto-install transmission |
| `TORSH_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, … |

## 🛠️ Development

```bash
pip install -e . -r requirements-dev.txt
pytest              # run the test suite
make check          # byte-compile sanity check
```

Torsh is built on [Textual](https://textual.textualize.io/) and talks to
Transmission via [`transmission-rpc`](https://github.com/Trim21/transmission-rpc).
The codebase is small and layered:

| Module | Responsibility |
|--------|----------------|
| `torsh/cli.py` | CLI entry point + boot sequence |
| `torsh/client.py` | Async, retrying wrapper over the Transmission RPC |
| `torsh/daemon.py` | Daemon discovery, install, launch & port selection |
| `torsh/config.py` | Typed, self-healing YAML configuration |
| `torsh/ui/` | Textual app, modals, stylesheet & boot console |

## 📝 License

MIT © Torsh authors
