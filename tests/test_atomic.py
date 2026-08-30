"""Atomic writes: the guarantees every persisted torsh file leans on."""

from __future__ import annotations

import os
import stat

import pytest

from torsh import atomic


def test_write_creates_parents_and_content(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.json"
    atomic.write_text(target, "hello ✓")
    assert target.read_text(encoding="utf-8") == "hello ✓"


def test_files_are_private_by_default(tmp_path):
    """Resume data says what you are downloading; the endpoint file holds a
    token. Neither is anyone else's business, whatever the umask says."""
    target = tmp_path / "secret"
    atomic.write_bytes(target, b"x")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_shared_mode_is_opt_in(tmp_path):
    target = tmp_path / "public.torrent"
    atomic.write_bytes(target, b"x", mode=atomic.SHARED)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_overwrite_replaces_in_place(tmp_path):
    target = tmp_path / "state"
    atomic.write_bytes(target, b"first")
    atomic.write_bytes(target, b"second")
    assert target.read_bytes() == b"second"
    assert list(tmp_path.iterdir()) == [target], "staging files must not survive"


def test_a_failed_write_leaves_the_previous_version_intact(tmp_path, monkeypatch):
    """The whole point of staging: a crash mid-write must not destroy data."""
    target = tmp_path / "state"
    atomic.write_bytes(target, b"good")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic.write_bytes(target, b"truncated")

    assert target.read_bytes() == b"good"
    assert list(tmp_path.iterdir()) == [target], "the staging file must be cleaned up"


def test_concurrent_writers_do_not_share_a_staging_name(tmp_path, monkeypatch):
    """Two processes saving the same file must not corrupt each other.

    The old `path.with_suffix('.tmp')` scheme gave both the identical staging
    path — one would write while the other renamed it away.
    """
    target = tmp_path / "config.toml"
    staged: list[str] = []
    real_replace = atomic.os.replace

    def spy(src, dst):
        staged.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", spy)
    atomic.write_text(target, "a = 1")
    atomic.write_text(target, "a = 2")

    assert len(set(staged)) == 2, staged
    assert target.read_text(encoding="utf-8") == "a = 2"


def test_sweep_stale_removes_only_leftovers(tmp_path):
    keeper = tmp_path / "abc.resume"
    keeper.write_bytes(b"keep me")
    (tmp_path / ".abc.resume.1234.tmp").write_bytes(b"interrupted")
    (tmp_path / ".hidden-but-not-ours").write_bytes(b"keep me too")

    assert atomic.sweep_stale(tmp_path) == 1
    assert keeper.exists()
    assert (tmp_path / ".hidden-but-not-ours").exists()
    assert atomic.sweep_stale(tmp_path) == 0


def test_sweep_stale_tolerates_a_missing_directory(tmp_path):
    assert atomic.sweep_stale(tmp_path / "gone") == 0
