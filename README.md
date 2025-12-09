# 🌊 Torsh — Transmission TUI

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/torsh.svg)](https://pypi.org/project/torsh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

A beautiful, modern TUI client for Transmission. One command, full control.

## ✨ Features

- 📊 **Dashboard View** — Sparklines, disk usage, real-time stats
- 🎯 **Smart Filters** — By name, status, or progress
- 📁 **File Browser** — View and prioritize individual files
- 🔗 **Tracker Info** — Monitor tracker status and peer counts
- 🔔 **Notifications** — Toast alerts when downloads complete
- ⌨️ **Vim Keys** — `j`/`k` navigation, power-user friendly
- 💾 **Session Persistence** — Remembers your filters and sort order
- 🚀 **Auto-Start** — Launches daemon automatically if needed

## 📦 Install

```bash
# Recommended (isolated environment)
pipx install torsh

# Or with pip
pip install torsh
```

Requires Python 3.10+. If `transmission-daemon` is missing, torsh can install it automatically.

## 🚀 Usage

```bash
torsh
```

That's it! Torsh will start the daemon if needed and connect automatically.

### Options

```bash
torsh --host localhost --port 9091
torsh --download-dir ~/Downloads/torrents
torsh --no-autostart  # Don't start daemon automatically
```

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `a` | Add magnet/torrent file |
| `d` | Delete torrent (with data) |
| `Space` | Pause/Resume |
| `s` | Set global speed limits |
| `p` | Set file priorities |
| `/` | Filter by name |
| `c` | Cycle status filter |
| `g` | Move download location |
| `?` | Show help |
| `q` | Quit |

**Navigation:** `j`/`k` scroll, `G` bottom, `Tab` switch panes

**Sorting:** Click headers or press `1`-`8`

## ⚙️ Configuration

Config is auto-created at `~/.config/torsh/config.yaml`.

Environment variables:
- `TORSH_HOST`, `TORSH_PORT` — RPC connection
- `TORSH_USER`, `TORSH_PASSWORD` — Authentication
- `TORSH_DOWNLOAD_DIR` — Default download directory
- `TORSH_LOG` — Log level (DEBUG, INFO, etc.)

## 📝 License

MIT © 2024
