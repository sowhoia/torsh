"""Shared fixtures: every test runs against an isolated torsh home."""

from __future__ import annotations

import threading
import time

import pytest

_ALERT_THREAD = "torsh-alerts"  # torsh.engine names its pump thread this


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point all torsh roots into pytest's tmp dir so tests never touch
    the developer's real config/state and can run in parallel.

    These live on the shared per-test ``monkeypatch``, so a test must never
    call ``monkeypatch.undo()``: that would drop this isolation too and aim
    the rest of the test at the developer's real daemon.  Use
    ``pytest.MonkeyPatch.context()`` for patches you need to lift early.
    """
    monkeypatch.setenv("TORSH_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TORSH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TORSH_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("TORSH_DOWNLOAD_DIR", raising=False)
    monkeypatch.delenv("TORSH_PORT", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def no_leaked_engines():
    """Fail the test that leaves a libtorrent session running behind it.

    Every :class:`~torsh.engine.Engine` owns an alert-pump thread, so a
    lingering one means a whole session — sockets, disk handles, a DHT node —
    escaped into the rest of the run.  That is how a green suite starts
    flaking, and it hides exactly the shutdown bugs these tests exist to
    catch.  Autouse, so it tears down after the daemon fixtures do.
    """
    before = {t.ident for t in threading.enumerate() if t.name == _ALERT_THREAD}
    yield

    def leaked() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == _ALERT_THREAD and t.ident not in before]

    # A joined pump can take a beat to actually retire; only a thread that is
    # still there after a grace period is a genuine leak.
    for _ in range(20):
        if not leaked():
            return
        time.sleep(0.1)
    pytest.fail(f"{len(leaked())} engine(s) outlived the test — something was never stopped")
