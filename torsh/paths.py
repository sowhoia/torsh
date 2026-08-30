"""Filesystem locations for torsh.

Everything lives in three XDG-style roots so users always know where to look:

* config  — ``~/.config/torsh``        (override: ``TORSH_CONFIG_DIR`` / ``XDG_CONFIG_HOME``)
* state   — ``~/.local/state/torsh``   (override: ``TORSH_STATE_DIR`` / ``XDG_STATE_HOME``)
* cache   — ``~/.cache/torsh``         (override: ``TORSH_CACHE_DIR`` / ``XDG_CACHE_HOME``)

State holds everything the daemon needs to survive restarts: pid/log files,
libtorrent session state and fast-resume data (which embeds the info dict,
so no separate ``.torrent`` cache is needed).
The RPC socket is the one exception — it lives in a short runtime dir (see
:func:`runtime_dir`) because ``AF_UNIX`` paths are length-capped.  All
lookups are functions (not module constants) so tests can re-point them
with a plain ``monkeypatch.setenv``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path


def _root(env_override: str, xdg_var: str, default: str) -> Path:
    override = os.environ.get(env_override)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get(xdg_var)
    base = Path(xdg).expanduser() if xdg else Path(default).expanduser()
    return base / "torsh"


def config_dir() -> Path:
    return _root("TORSH_CONFIG_DIR", "XDG_CONFIG_HOME", "~/.config")


def state_dir() -> Path:
    return _root("TORSH_STATE_DIR", "XDG_STATE_HOME", "~/.local/state")


def cache_dir() -> Path:
    return _root("TORSH_CACHE_DIR", "XDG_CACHE_HOME", "~/.cache")


def config_file() -> Path:
    return config_dir() / "config.toml"


def legacy_config_file() -> Path:
    """Location of the pre-1.0 YAML config, read once for migration."""
    return config_dir() / "config.yaml"


# ---------------------------------------------------------------------------
# Daemon runtime files
# ---------------------------------------------------------------------------


def runtime_dir() -> Path:
    """Short per-user directory for the RPC socket, tmux-style.

    ``AF_UNIX`` paths are capped at ~104 bytes on macOS/BSD, so the socket
    cannot live under an arbitrary (possibly deep) state dir.  The location
    is deliberately *not* derived from ``$XDG_RUNTIME_DIR``: an env-dependent
    path would make a cron/ssh session (no XDG vars) miss the desktop
    session's daemon and autostart a second one over the same state dir.
    :func:`ensure_dirs` verifies ownership before use, like tmux does.
    """
    # getuid is POSIX-only; on Windows gettempdir() is already per-user.
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"torsh-{uid}"


def socket_path() -> Path:
    """Unix socket the daemon listens on (override: ``TORSH_SOCKET``).

    The name embeds a digest of the state dir so separate torsh homes
    (tests, ad-hoc ``TORSH_STATE_DIR`` setups) get separate daemons.
    """
    override = os.environ.get("TORSH_SOCKET")
    if override:
        return Path(override).expanduser()
    # Resolve first: `TORSH_STATE_DIR=./state` must digest the same from any cwd.
    canonical = str(state_dir().expanduser().resolve())
    digest = hashlib.sha1(canonical.encode(), usedforsecurity=False).hexdigest()[:10]
    return runtime_dir() / f"daemon-{digest}.sock"


def pid_file() -> Path:
    return state_dir() / "daemon.pid"


def daemon_log_file() -> Path:
    return state_dir() / "daemon.log"


def session_state_file() -> Path:
    return state_dir() / "session" / "state.dat"


def resume_dir() -> Path:
    return state_dir() / "session" / "resume"


def ids_file() -> Path:
    return state_dir() / "session" / "ids.json"


def default_download_dir() -> Path:
    return Path("~/Downloads/torsh").expanduser()


def ensure_dirs() -> None:
    """Create every directory torsh writes to (idempotent)."""
    for d in (config_dir(), state_dir(), cache_dir(), resume_dir()):
        d.mkdir(parents=True, exist_ok=True)
    # State can contain resume data for private torrents; keep it to the user.
    # Best-effort: a deliberately shared/foreign state dir must not be fatal.
    with contextlib.suppress(OSError):
        os.chmod(state_dir(), 0o700)

    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "getuid"):
        # The runtime dir lives under a world-writable tmpdir: refuse to
        # place a socket inside a directory someone else pre-created.
        owner = runtime.stat().st_uid
        if owner != os.getuid():
            raise PermissionError(f"{runtime} is owned by uid {owner}, not you — refusing to use it")
    os.chmod(runtime, 0o700)
