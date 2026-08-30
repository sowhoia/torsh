"""Daemon + IPC integration: a real server process, a real socket.

The daemon runs in-process (asyncio task) for speed and determinism; the
sync client talks to it from a worker thread exactly like the CLI would.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import threading
import time

import pytest

from tests.test_engine import make_torrent
from torsh import config as config_mod
from torsh import daemon as daemon_mod
from torsh import ipc, paths


def _quiet_config(tmp_path):
    """A daemon that talks to nobody: no DHT, no port mapping, no LSD."""
    config = config_mod.load()
    config.downloads.dir = str(tmp_path / "dl")
    config.network.dht = False
    config.network.upnp = False
    config.network.natpmp = False
    config.network.lsd = False
    config_mod.save(config)
    return config


def _serve_in_background(config):
    """Run a real daemon on its own event loop until the test is done."""
    loop = asyncio.new_event_loop()
    done = threading.Event()

    def runner():
        try:
            loop.run_until_complete(daemon_mod.serve(config))
        finally:
            loop.close()
            done.set()

    threading.Thread(target=runner, daemon=True).start()

    deadline = time.time() + 15
    while time.time() < deadline:
        if ipc.daemon_running():
            break
        time.sleep(0.1)
    else:
        raise AssertionError("daemon did not come up")

    try:
        yield config
    finally:
        # Ungated on purpose: a test may have patched the very RPC that
        # reports the daemon as alive, and a daemon that outlives its test
        # leaks a whole libtorrent session into the rest of the run.
        daemon_mod.stop_daemon(timeout=15)
        assert done.wait(timeout=20), "the daemon under test outlived it"


@pytest.fixture
def running_daemon(tmp_path):
    """The normal transport: a private Unix socket."""
    yield from _serve_in_background(_quiet_config(tmp_path))


@pytest.fixture
def tcp_daemon(tmp_path, monkeypatch):
    """The Windows fallback: token-guarded loopback TCP, forced on here so
    the path gets exercised on the platforms CI actually runs."""
    monkeypatch.setattr(ipc, "use_tcp", lambda: True)
    yield from _serve_in_background(_quiet_config(tmp_path))


def test_ping_and_session(running_daemon):
    with ipc.Client() as client:
        pong = client.call("ping")
        assert pong["pid"] > 0
        session = client.call("session")
        assert session["torrents"] == 0
        assert session["engine"].startswith("libtorrent")


def test_snapshot_bundles_list_and_session(running_daemon):
    with ipc.Client() as client:
        snapshot = client.call("snapshot")
        assert snapshot["torrents"] == []
        assert snapshot["session"]["engine"].startswith("libtorrent")


def test_duplicate_add_returns_structured_hash(running_daemon, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "dup.torrent").write_bytes(meta)
    with ipc.Client() as client:
        view = client.call("add", uri=str(tmp_path / "dup.torrent"), paused=True)
        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("add", uri=str(tmp_path / "dup.torrent"), paused=True)
        assert excinfo.value.code == "duplicate"
        assert excinfo.value.data["hash"] == view["hash"]


def test_add_list_get_remove_over_rpc(running_daemon, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    torrent_file = tmp_path / "rpc.torrent"
    torrent_file.write_bytes(meta)

    with ipc.Client() as client:
        view = client.call("add", uri=str(torrent_file), paused=True)
        assert view["name"] == "payload.bin"
        assert view["id"] == 1

        torrents = client.call("list")
        assert len(torrents) == 1

        detail = client.call("get", hash=view["hash"])
        assert detail["piece_count"] > 0

        files = client.call("files", hash=view["hash"])
        assert files[0]["path"] == "payload.bin"

        assert client.call("remove", hashes=[view["hash"]], delete_data=True) == 1
        assert client.call("list") == []


def test_errors_carry_codes(running_daemon):
    with ipc.Client() as client:
        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("get", hash="f" * 40)
        assert excinfo.value.code == "not_found"

        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("frobnicate")
        assert excinfo.value.code == "unknown_method"

        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("add", uri="not a torrent at all")
        assert excinfo.value.code == "invalid"

        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("get")  # missing required param
        assert excinfo.value.code == "bad_params"


def _raise_type_error(self):
    raise TypeError("an internal bug, not a caller mistake")


def test_internal_typeerror_is_not_blamed_on_the_caller(running_daemon, monkeypatch):
    """`bad_params` must mean "you called it wrong", nothing else.

    Params are bound against the handler's signature *before* the call, so a
    TypeError from inside the handler stays an `internal` error (and keeps
    its traceback in the daemon log).
    """
    monkeypatch.setattr(daemon_mod.Service, "rpc_ping", _raise_type_error)
    with ipc.Client() as client, pytest.raises(ipc.RpcError) as excinfo:
        client.call("ping")
    assert excinfo.value.code == "internal"


def test_global_limits_persist_to_config(running_daemon):
    with ipc.Client() as client:
        limits = client.call("global_limits", down=2 * 1024**2, up=512 * 1024)
        assert limits == {"down": 2 * 1024**2, "up": 512 * 1024}
    saved = config_mod.load()
    assert saved.limits.download == 2 * 1024**2
    assert saved.limits.upload == 512 * 1024


def test_async_client(running_daemon):
    async def scenario():
        client = ipc.AsyncClient()
        pong = await client.call("ping")
        torrents = await client.call("list")
        await client.close()
        return pong, torrents

    pong, torrents = asyncio.run(scenario())
    assert pong["pid"] > 0
    assert torrents == []


def test_stop_daemon(running_daemon):
    assert daemon_mod.stop_daemon(timeout=20) is True
    assert not ipc.daemon_running()
    assert not paths.socket_path().exists()
    assert not paths.pid_file().exists()


def test_running_pid_is_the_daemon_s_own_answer(running_daemon):
    """Asked over RPC, not read from a file that a crash could have staled."""
    assert daemon_mod.running_pid() == os.getpid()
    assert paths.pid_file().read_text(encoding="utf-8").strip() == str(os.getpid())


def test_stop_escalates_to_sigterm_when_shutdown_is_ignored(running_daemon):
    """A wedged daemon must still be stoppable — that is what SIGTERM is for.

    The daemon under test lives in *this* process, so the signal itself is
    stubbed; what is asserted is that torsh escalates, and aims correctly.
    """
    signalled: list[tuple[int, int]] = []
    # Its own context, not the `monkeypatch` fixture: these patches have to be
    # lifted *before* the fixture stops the daemon (it needs a working
    # `shutdown`), and the shared fixture is only undone afterwards.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon_mod.Service, "rpc_shutdown", lambda self: True)  # accepted, ignored
        patch.setattr(daemon_mod, "_TERM_GRACE", 0.5)
        patch.setattr(daemon_mod.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        assert daemon_mod.stop_daemon(timeout=1.0) is False  # our fake kill did nothing

    assert signalled == [(os.getpid(), signal.SIGTERM)]


def test_stop_sweeps_after_a_daemon_that_died_without_cleaning_up(running_daemon):
    """SIGKILL, or a power cut: the files outlive the process.

    `stop` has to clear them, otherwise the next start trips over a socket
    with nothing behind it.
    """
    assert daemon_mod.stop_daemon(timeout=20) is True
    paths.pid_file().write_text("4242\n", encoding="utf-8")
    paths.socket_path().touch()

    assert daemon_mod.stop_daemon(timeout=2) is True
    assert not paths.pid_file().exists()
    assert not paths.socket_path().exists()


# ---------------------------------------------------------------------------
# The TCP fallback transport (used on Windows, forced on here)
# ---------------------------------------------------------------------------


def test_tcp_transport_works_end_to_end(tcp_daemon):
    port, token = ipc.read_tcp_endpoint()
    assert 0 < port < 65536
    assert len(token) == 32
    with ipc.Client() as client:
        assert client.call("ping")["pid"] == os.getpid()
        assert client.call("list") == []


def test_tcp_endpoint_file_is_private(tcp_daemon):
    """It holds the auth token — nobody else on the box may read it."""
    assert stat.S_IMODE(ipc.endpoint_file().stat().st_mode) == 0o600


@pytest.mark.parametrize("presented", ["wrong-token", "", "0" * 32])
def test_tcp_transport_rejects_a_bad_token(tcp_daemon, presented):
    port, real = ipc.read_tcp_endpoint()
    assert presented != real
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(json.dumps({"auth": presented}).encode() + b"\n")
        sock.sendall(ipc.encode_request(1, "ping", {}))
        sock.settimeout(5)
        assert _hung_up_on(sock), "an unauthenticated peer must get nothing back"
    finally:
        sock.close()
    with ipc.Client() as client:  # ...and the daemon is unharmed
        assert client.call("ping")["pid"] == os.getpid()


def test_tcp_transport_hangs_up_on_a_silent_client(tcp_daemon, monkeypatch):
    """A peer that connects and never authenticates must not hold a slot."""
    monkeypatch.setattr(daemon_mod, "_AUTH_TIMEOUT", 0.3)
    port, _ = ipc.read_tcp_endpoint()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.settimeout(5)
        assert _hung_up_on(sock), "the daemon should have closed the connection"
    finally:
        sock.close()


def _hung_up_on(sock: socket.socket) -> bool:
    """Did the peer close without answering?

    Closing a socket that still has unread data queued makes the OS send RST
    rather than FIN, so a refusal shows up as either a clean EOF or a reset
    depending on timing. Both mean the same thing.
    """
    try:
        return sock.recv(4096) == b""
    except ConnectionResetError:
        return True


def test_stop_daemon_with_idle_client(running_daemon):
    """An idle connection must not stall shutdown.

    Python ≥ 3.12 makes Server.wait_closed() await every live handler; the
    daemon closes client writers itself, so stop stays fast.
    """
    idle = ipc.Client()
    idle.connect()
    try:
        started = time.time()
        assert daemon_mod.stop_daemon(timeout=15) is True
        assert time.time() - started < 10.0, "shutdown stalled behind an idle client"
        assert not ipc.daemon_running()
    finally:
        idle.close()
