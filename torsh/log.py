"""Logging setup.

Frontends (CLI/TUI) log to ``~/.cache/torsh/torsh.log``; the daemon logs to
``<state>/daemon.log``.  Nothing ever logs to stdout — that belongs to the
user — unless ``TORSH_DEBUG=1`` asks for it explicitly.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from . import paths

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 2 * 1024 * 1024  # rotate at 2 MiB, keep 3 old files
_BACKUPS = 3
_configured_root: Path | None = None


def _level() -> int:
    name = os.environ.get("TORSH_LOG_LEVEL", "INFO").upper()
    return getattr(logging, name, logging.INFO)


def setup(logfile: Path | None = None) -> None:
    """Route the ``torsh`` logger tree to *logfile* (defaults to the cache log)."""
    global _configured_root
    root = logging.getLogger("torsh")
    root.setLevel(_level())
    root.propagate = False

    target = logfile or (paths.cache_dir() / "torsh.log")
    if _configured_root == target and root.handlers:
        return
    for stale in list(root.handlers):
        root.removeHandler(stale)

    handler: logging.Handler
    if os.environ.get("TORSH_DEBUG"):
        handler = logging.StreamHandler()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Rotation keeps a long-lived daemon's log bounded (~8 MiB worst case);
        # delay=True skips the file open for CLI runs that never log a line.
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=True
        )
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    _configured_root = target


def get(name: str) -> logging.Logger:
    """Logger factory: ``log.get(__name__)``."""
    setup()
    if name.startswith("torsh"):
        return logging.getLogger(name)
    return logging.getLogger(f"torsh.{name}")
