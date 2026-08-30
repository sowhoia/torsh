"""Client-side RPC semantics, driven by a daemon we can make misbehave.

The interesting logic in :mod:`torsh.ipc` is what happens when a call goes
wrong: a request that never left the process is always safe to re-send, but
one whose *reply* was lost may already have executed — re-sending `add`
there would silently add a torrent twice.  A real daemon is a bad place to
test that, so this file brings its own.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from torsh import ipc


class FakeDaemon:
    """A minimal RPC server whose per-request behaviour is programmable."""

    def __init__(self, path: Path, behaviour: str = "reply") -> None:
        self.behaviour = behaviour
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self._lock = threading.Lock()
        self._closing = False
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while not self._closing:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as reader:
            for line in reader:
                request = json.loads(line)
                with self._lock:
                    self.requests.append(request)
                    seen = len(self.requests)
                if self.behaviour == "drop_first" and seen == 1:
                    return  # hang up without answering: the reply is "lost"
                if self.behaviour == "never_reply":
                    return
                if self.behaviour == "oversized":
                    conn.sendall(b"x" * 4096)  # no newline: the line never ends
                    return
                reply = json.dumps({"id": request["id"], "ok": True, "result": "pong"}) + "\n"
                conn.sendall(reply.encode())

    def close(self) -> None:
        self._closing = True
        self._sock.close()


@pytest.fixture
def daemon_at(monkeypatch):
    """Point torsh at a FakeDaemon on a short-enough socket path."""
    directory = Path(tempfile.mkdtemp(prefix="torsh-rpc-"))  # AF_UNIX caps paths at ~104 bytes
    monkeypatch.setenv("TORSH_SOCKET", str(directory / "rpc.sock"))
    servers: list[FakeDaemon] = []

    def start(behaviour: str = "reply") -> FakeDaemon:
        server = FakeDaemon(directory / "rpc.sock", behaviour)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()
    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_call_roundtrips_and_reuses_one_connection(daemon_at):
    server = daemon_at()
    with ipc.Client() as client:
        assert client.call("ping") == "pong"
        assert client.call("list") == "pong"
    assert server.connections == 1, "calls must share the persistent connection"
    assert [r["method"] for r in server.requests] == ["ping", "list"]
    assert [r["id"] for r in server.requests] == [1, 2], "ids must not repeat"


def test_errors_become_rpc_errors_with_their_code(daemon_at):
    daemon_at()
    payload = {"ok": False, "error": {"code": "not_found", "message": "nope", "data": {"hash": "ab"}}}
    with pytest.raises(ipc.RpcError) as excinfo:
        ipc._raise_on_error(payload)
    assert excinfo.value.code == "not_found"
    assert excinfo.value.data == {"hash": "ab"}


def test_decode_message_rejects_non_objects():
    assert ipc.decode_message(b'{"ok": true}') == {"ok": True}
    with pytest.raises(ValueError, match="JSON objects"):
        ipc.decode_message(b"[1, 2, 3]")


# ---------------------------------------------------------------------------
# Failure handling — the part that decides whether a retry is safe
# ---------------------------------------------------------------------------


def test_a_request_that_never_left_is_retried_transparently(daemon_at, monkeypatch):
    server = daemon_at()
    client = ipc.Client()
    client.connect()

    real_sendall = socket.socket.sendall
    broken = {"once": False}

    def flaky(self, data):
        if not broken["once"]:
            broken["once"] = True
            raise BrokenPipeError("the daemon restarted under us")
        return real_sendall(self, data)

    monkeypatch.setattr(socket.socket, "sendall", flaky)
    try:
        assert client.call("add", uri="magnet:?x") == "pong"
    finally:
        client.close()
    # Even `add` is safe here: the bytes provably never reached the daemon.
    assert len(server.requests) == 1


def test_a_lost_reply_is_retried_only_for_idempotent_methods(daemon_at):
    server = daemon_at("drop_first")
    with ipc.Client() as client:
        assert client.call("list") == "pong"
    assert len(server.requests) == 2, "a lost `list` reply should be re-asked"


@pytest.mark.parametrize("method", sorted(ipc._NO_RETRY_AFTER_SEND))
def test_a_lost_reply_is_never_retried_for_mutations(daemon_at, method):
    """`add`/`remove` may have executed already — a blind retry would either
    add a torrent twice or report a bogus "not found" for a real removal."""
    server = daemon_at("drop_first")
    with ipc.Client() as client, pytest.raises(ipc.RpcError) as excinfo:
        client.call(method)
    assert excinfo.value.code == "connection_lost"
    assert method in str(excinfo.value)
    assert len(server.requests) == 1, "the request must not be replayed"


def test_an_oversized_reply_drops_the_connection_instead_of_desyncing(daemon_at, monkeypatch):
    monkeypatch.setattr(ipc, "MAX_MESSAGE", 1024)
    daemon_at("oversized")
    client = ipc.Client()
    try:
        with pytest.raises(ipc.RpcError) as excinfo:
            client.call("list")
        assert excinfo.value.code == "oversized"
        # The tail of that reply is still in flight; reusing the socket would
        # line every later call up against the wrong response.
        assert client.connected is False
    finally:
        client.close()


def test_an_unreachable_daemon_is_reported_not_retried_forever(monkeypatch, tmp_path):
    monkeypatch.setenv("TORSH_SOCKET", str(tmp_path / "nothing-here.sock"))
    with pytest.raises(ipc.DaemonUnavailable):
        ipc.Client().call("ping")
    assert ipc.daemon_running() is False


def test_ensure_daemon_reports_a_child_that_dies_at_once(monkeypatch, tmp_path):
    """A daemon that exits on startup must not cost the user the full wait."""

    class DeadProcess:
        def poll(self) -> int:
            return 3

    monkeypatch.setenv("TORSH_SOCKET", str(tmp_path / "never.sock"))
    monkeypatch.setattr(ipc, "spawn_daemon", lambda: DeadProcess())
    (tmp_path / "log").write_text("Traceback\nImportError: no libtorrent here\n", encoding="utf-8")
    monkeypatch.setattr(ipc.paths, "daemon_log_file", lambda: tmp_path / "log")

    with pytest.raises(ipc.DaemonUnavailable) as excinfo:
        ipc.ensure_daemon(wait=60.0)  # would hang for a minute without the poll
    assert "status 3" in str(excinfo.value)
    assert "no libtorrent here" in str(excinfo.value), "the daemon's last words help"


def test_ensure_daemon_respects_autostart_off(monkeypatch, tmp_path):
    monkeypatch.setenv("TORSH_SOCKET", str(tmp_path / "never.sock"))
    monkeypatch.setattr(ipc, "spawn_daemon", lambda: pytest.fail("must not spawn"))
    with pytest.raises(ipc.DaemonUnavailable):
        ipc.ensure_daemon(autostart=False)


# ---------------------------------------------------------------------------
# The asyncio client (the TUI's side) must make the same promises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_client_roundtrips_and_closes(daemon_at):
    server = daemon_at()
    client = ipc.AsyncClient()
    assert client.connected is False
    assert await client.call("ping") == "pong"
    assert client.connected is True
    await client.close()
    assert client.connected is False
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_async_client_retries_an_idempotent_call_after_a_lost_reply(daemon_at):
    server = daemon_at("drop_first")
    client = ipc.AsyncClient()
    try:
        assert await client.call("list") == "pong"
    finally:
        await client.close()
    assert len(server.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(ipc._NO_RETRY_AFTER_SEND))
async def test_async_client_never_replays_a_mutation(daemon_at, method):
    """The TUI's `add` must not double-add just because a reply went missing."""
    server = daemon_at("drop_first")
    client = ipc.AsyncClient()
    try:
        with pytest.raises(ipc.RpcError) as excinfo:
            await client.call(method)
        assert excinfo.value.code == "connection_lost"
    finally:
        await client.close()
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_async_client_reports_an_unreachable_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("TORSH_SOCKET", str(tmp_path / "nothing.sock"))
    client = ipc.AsyncClient()
    with pytest.raises(ipc.DaemonUnavailable):
        await client.call("ping")
