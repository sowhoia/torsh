"""Filesystem locations: XDG roots, runtime dir, socket naming, log setup."""

from __future__ import annotations

import logging
import logging.handlers
import os

from torsh import log as tlog
from torsh import paths


def test_roots_follow_torsh_env(isolated_home):
    assert paths.config_dir() == isolated_home / "config"
    assert paths.state_dir() == isolated_home / "state"
    assert paths.cache_dir() == isolated_home / "cache"
    assert paths.config_file().parent == paths.config_dir()


def test_socket_path_isolates_state_dirs(monkeypatch, tmp_path):
    """Different torsh homes must resolve to different daemon sockets."""
    monkeypatch.setenv("TORSH_STATE_DIR", str(tmp_path / "one"))
    first = paths.socket_path()
    monkeypatch.setenv("TORSH_STATE_DIR", str(tmp_path / "two"))
    second = paths.socket_path()
    assert first != second
    assert first.suffix == ".sock"


def test_socket_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TORSH_SOCKET", str(tmp_path / "custom.sock"))
    assert paths.socket_path() == tmp_path / "custom.sock"


def test_runtime_dir_survives_missing_getuid(monkeypatch):
    """Windows has no os.getuid — the fallback must still work."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delattr(os, "getuid")
    assert paths.runtime_dir().name == "torsh-user"


def test_ensure_dirs_idempotent_and_private(isolated_home):
    paths.ensure_dirs()
    paths.ensure_dirs()
    assert paths.resume_dir().is_dir()
    assert (paths.state_dir().stat().st_mode & 0o777) == 0o700


def test_log_setup_rotates(isolated_home, monkeypatch):
    monkeypatch.delenv("TORSH_DEBUG", raising=False)
    target = paths.state_dir() / "daemon.log"
    tlog.setup(target)
    root = logging.getLogger("torsh")
    rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert rotating, "daemon logs must be size-bounded"
    assert rotating[0].maxBytes > 0
    assert rotating[0].backupCount > 0
