"""The torsh daemon: owns the engine, serves the JSON-RPC socket.

Started explicitly with ``torsh daemon run`` (foreground, systemd-friendly)
or spawned on demand by any CLI/TUI command via :func:`torsh.ipc.ensure_daemon`.
Downloads keep running after every frontend exits; ``torsh daemon stop``
shuts it down cleanly (saving fast-resume data for every torrent).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import secrets
import signal
import socket
import time
from pathlib import Path
from typing import Any

from . import __version__, atomic, ipc, log, paths
from . import config as config_mod
from .engine import Engine, EngineError

LOG = log.get(__name__)

_AUTH_TIMEOUT = 5.0  # seconds a TCP client gets to present its token
_TERM_GRACE = 10.0  # seconds to wait after escalating to SIGTERM


class Service:
    """RPC method registry around one :class:`Engine`."""

    def __init__(self, config: config_mod.Config) -> None:
        self.config = config
        self.engine = Engine(config)
        self.started_at = time.time()
        self.connections = 0
        self.shutdown_event = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Live client connections. On 3.12+ Server.wait_closed() blocks until
        # every handler finishes, so shutdown must be able to close them.
        self.writers: set[asyncio.StreamWriter] = set()

    # Every rpc_* method runs inside asyncio.to_thread — the engine is
    # synchronous and thread-safe, so slow calls never block the loop.

    def rpc_ping(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime": int(time.time() - self.started_at),
            "clients": self.connections,
        }

    def rpc_list(self) -> list[dict[str, Any]]:
        return self.engine.torrents()

    def rpc_get(self, hash: str) -> dict[str, Any]:
        return self.engine.get(hash)

    def rpc_add(
        self,
        uri: str,
        dir: str | None = None,
        paused: bool = False,
        sequential: bool | None = None,
    ) -> dict[str, Any]:
        return self.engine.add(uri, download_dir=dir, paused=bool(paused), sequential=sequential)

    def rpc_pause(self, hashes: list[str]) -> int:
        return self.engine.pause(hashes)

    def rpc_resume(self, hashes: list[str]) -> int:
        return self.engine.resume(hashes)

    def rpc_recheck(self, hashes: list[str]) -> int:
        return self.engine.recheck(hashes)

    def rpc_reannounce(self, hashes: list[str]) -> int:
        return self.engine.reannounce(hashes)

    def rpc_remove(self, hashes: list[str], delete_data: bool = False) -> int:
        return self.engine.remove(hashes, delete_data=bool(delete_data))

    def rpc_move(self, hash: str, dir: str) -> bool:
        self.engine.move(hash, dir)
        return True

    def rpc_sequential(self, hash: str, enabled: bool) -> bool:
        self.engine.set_sequential(hash, bool(enabled))
        return True

    def rpc_queue(self, hash: str, move: str) -> int:
        return self.engine.set_queue_position(hash, str(move))

    def rpc_export(self, hash: str, target: str) -> str:
        return self.engine.export(hash, target)

    def rpc_files(self, hash: str) -> list[dict[str, Any]]:
        return self.engine.files(hash)

    def rpc_priorities(self, hash: str, changes: dict[str, Any]) -> list[int]:
        return self.engine.set_file_priorities(hash, changes)

    def rpc_peers(self, hash: str) -> list[dict[str, Any]]:
        return self.engine.peers(hash)

    def rpc_trackers(self, hash: str) -> list[dict[str, Any]]:
        return self.engine.trackers(hash)

    def rpc_torrent_limits(self, hash: str, down: int | None = None, up: int | None = None) -> dict[str, int]:
        if down is not None or up is not None:
            self.engine.set_torrent_limits(hash, down, up)
        return self.engine.get_torrent_limits(hash)

    def rpc_global_limits(self, down: int | None = None, up: int | None = None) -> dict[str, int]:
        if down is not None or up is not None:
            self.engine.set_global_limits(down, up)
            # Persist intent: reload-modify-save so other keys aren't clobbered
            # (env=False — the daemon's TORSH_* overrides must not be baked in).
            fresh = config_mod.load(env=False)
            fresh.limits.download = self.engine.config.limits.download
            fresh.limits.upload = self.engine.config.limits.upload
            config_mod.save(fresh)
        limits = self.engine.config.limits
        return {"down": limits.download, "up": limits.upload}

    def rpc_session(self) -> dict[str, Any]:
        return self.engine.session_info()

    def rpc_snapshot(self) -> dict[str, Any]:
        """Torrent list + session stats in one round-trip (the TUI's tick)."""
        return {"torrents": self.engine.torrents(), "session": self.engine.session_info()}

    def rpc_connect(self, hash: str, host: str, port: int) -> bool:
        """Introduce a peer by hand (debug/testing aid, e.g. LAN seedboxes)."""
        self.engine.connect_peer(hash, host, int(port))
        return True

    def rpc_reload(self) -> bool:
        fresh = config_mod.load()
        self.config = fresh
        self.engine.apply_config(fresh)
        return True

    def rpc_shutdown(self) -> bool:
        # Runs in a worker thread; hop back to the loop so the "ok" reply
        # flushes before the server starts tearing connections down.
        if self.loop is not None:
            self.loop.call_soon_threadsafe(
                lambda: self.loop.call_later(0.1, self.shutdown_event.set)  # type: ignore[union-attr]
            )
        return True

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handler = getattr(self, f"rpc_{method}", None)
        if handler is None or not callable(handler):
            raise EngineError(f"unknown method {method!r}", code="unknown_method")
        try:
            bound = inspect.signature(handler).bind(**params)
        except TypeError as exc:
            # Bind first so a *caller's* mistake is reported as bad_params —
            # a TypeError raised inside the handler is our bug, not theirs,
            # and must keep its "internal" code and traceback.
            raise EngineError(str(exc), code="bad_params") from exc
        return handler(*bound.args, **bound.kwargs)


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


async def _handle_connection(
    service: Service,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    token: str | None,
) -> None:
    service.connections += 1
    service.writers.add(writer)
    try:
        if token is not None:  # TCP transport requires an auth line first
            try:
                # Bounded: an unauthenticated peer that connects and then says
                # nothing must not hold a connection slot indefinitely.
                line = await asyncio.wait_for(reader.readline(), timeout=_AUTH_TIMEOUT)
                auth = ipc.decode_message(line)
            except (TimeoutError, asyncio.TimeoutError, ValueError, json.JSONDecodeError, OSError):
                return
            presented = auth.get("auth")
            # compare_digest, not ==: token comparison must not leak the
            # matching prefix length through timing.
            if not isinstance(presented, str) or not secrets.compare_digest(presented, token):
                LOG.warning("rejected a connection with a bad auth token")
                return

        while not service.shutdown_event.is_set():
            try:
                line = await reader.readline()
            except (ValueError, ConnectionResetError, asyncio.LimitOverrunError):
                break  # oversized or torn message: drop the connection
            if not line:
                break
            response = await _process_line(service, line)
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        service.connections -= 1
        service.writers.discard(writer)
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def _process_line(service: Service, line: bytes) -> dict[str, Any]:
    request_id: Any = None
    try:
        message = ipc.decode_message(line)
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if not isinstance(params, dict):
            raise EngineError("params must be an object", code="invalid")
        result = await asyncio.to_thread(service.dispatch, method, params)
        return {"id": request_id, "ok": True, "result": result}
    except EngineError as exc:
        error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.data:
            error["data"] = exc.data
        return {"id": request_id, "ok": False, "error": error}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return {"id": request_id, "ok": False, "error": {"code": "bad_request", "message": str(exc)}}
    except Exception as exc:
        LOG.exception("internal error")
        return {"id": request_id, "ok": False, "error": {"code": "internal", "message": str(exc)}}


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


async def serve(config: config_mod.Config) -> None:
    # Probing costs a blocking connect + ping; off-loaded because `serve` may
    # be driven by a loop that has other work (tests, embedders).
    if await asyncio.to_thread(ipc.daemon_running):
        raise SystemExit("torsh daemon is already running")

    paths.ensure_dirs()
    service = Service(config)
    service.loop = asyncio.get_running_loop()
    LOG.info("starting engine (torsh %s)", __version__)
    await asyncio.to_thread(service.engine.start)

    server: asyncio.AbstractServer | None = None
    try:
        token: str | None = None
        if ipc.use_tcp():  # pragma: no cover - windows path
            server = await asyncio.start_server(
                lambda r, w: _handle_connection(service, r, w, token=token),
                "127.0.0.1",
                0,
                limit=ipc.MAX_MESSAGE,
            )
            port = server.sockets[0].getsockname()[1]
            token = ipc.write_tcp_endpoint(port)
        else:
            sock_path = paths.socket_path()
            await asyncio.to_thread(_remove_stale_socket, sock_path)
            server = await asyncio.start_unix_server(
                lambda r, w: _handle_connection(service, r, w, token=None),
                path=str(sock_path),
                limit=ipc.MAX_MESSAGE,
            )
            os.chmod(sock_path, 0o600)

        # Atomic, so a supervisor reading it never sees a truncated number.
        atomic.write_text(paths.pid_file(), f"{os.getpid()}\n")

        # Signal handlers only exist on the main thread (tests run the loop
        # elsewhere) — RuntimeError there, NotImplementedError on Windows.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, service.shutdown_event.set)

        LOG.info("daemon ready (pid %d)", os.getpid())
        await service.shutdown_event.wait()
    finally:
        LOG.info("shutting down")
        if server is not None:
            server.close()
            # An idle client parked in readline() would otherwise stall
            # wait_closed() (which awaits every handler on Python ≥ 3.12).
            for writer in list(service.writers):
                writer.close()
            await server.wait_closed()
        # On < 3.12 wait_closed() returns without awaiting handlers — give
        # in-flight requests a moment to finish before the engine goes away.
        for _ in range(100):
            if service.connections == 0:
                break
            await asyncio.sleep(0.05)
        else:
            LOG.warning("stopping with %d request(s) still in flight", service.connections)
        await asyncio.to_thread(service.engine.stop)
        if server is not None:
            # Only the daemon that bound the endpoint may sweep it: the loser
            # of an autostart race (its own probe passed, then the bind found
            # the winner) exits here too, and unlinking would sabotage the
            # winner's socket and pid file.
            _cleanup_files()
        LOG.info("bye")


def _remove_stale_socket(sock_path: Path) -> None:
    if not sock_path.exists():
        return
    # A refused connect usually means a dead daemon — but it is also what a
    # peer looks like in the brief window between creating the socket file
    # and listen(). Probe a few times before concluding it is stale: deleting
    # a *starting* daemon's socket would orphan it mid-autostart.
    for attempt in range(3):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(str(sock_path))
        except OSError:
            if attempt == 2:
                sock_path.unlink(missing_ok=True)
                return
            time.sleep(0.25)
        else:
            raise SystemExit("torsh daemon is already running")
        finally:
            probe.close()


def _cleanup_files() -> None:
    paths.pid_file().unlink(missing_ok=True)
    if not ipc.use_tcp():
        paths.socket_path().unlink(missing_ok=True)
    else:  # pragma: no cover - windows path
        ipc.endpoint_file().unlink(missing_ok=True)


def run_daemon(config: config_mod.Config) -> None:
    """Blocking entry point for ``torsh daemon run``."""
    log.setup(paths.daemon_log_file())
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover
        asyncio.run(serve(config))


def running_pid() -> int | None:
    """The live daemon's pid, straight out of its own ``ping`` reply.

    Deliberately not the pid file: that can be stale after a crash, and a
    signal aimed at a recycled pid would land on an unrelated process.  If
    the daemon can still answer, its answer is the only trustworthy source.
    """
    client = ipc.Client(call_timeout=3.0)
    try:
        return int(client.call("ping")["pid"])
    except (ipc.DaemonUnavailable, ipc.RpcError, OSError, KeyError, TypeError, ValueError):
        return None
    finally:
        client.close()


def stop_daemon(timeout: float = 20.0) -> bool:
    """Ask the daemon to exit and wait until it is fully gone.

    "Gone" means the RPC endpoint no longer answers *and* its files are
    cleaned up, so a `stop && start` sequence never races the old process.
    A daemon that ignores the request is escalated to SIGTERM.
    """
    client = ipc.Client()
    try:
        client.connect(timeout=2.0)
        client.call("shutdown")
    except (ipc.DaemonUnavailable, ipc.RpcError, OSError):
        if ipc.daemon_running():
            return False
    finally:
        client.close()

    if _settled(timeout):
        return True

    # Still answering long after a clean request: the event loop is wedged
    # (a disk stuck mid resume-sweep, say). SIGTERM trips the very same
    # shutdown path, so resume data is still saved — it only skips the reply.
    # (POSIX only: Windows maps os.kill to TerminateProcess, which skips the
    # handler and loses the last resume interval; supported installs there
    # run under WSL.)
    pid = running_pid()
    if pid is None:
        return False
    LOG.warning("daemon %d ignored `shutdown` — escalating to SIGTERM", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:  # already gone, or not ours to signal
        LOG.warning("could not signal daemon %d: %s", pid, exc)
        return _settled(0.0)
    return _settled(_TERM_GRACE)


def _settled(timeout: float) -> bool:
    """Wait for the daemon to be gone *and* tidy; sweep after it if it isn't.

    All three conditions matter: `torsh daemon restart` must not hand the new
    daemon a socket the old one is about to unlink, and the dying daemon's
    own sweep must not delete the *replacement's* pid file.  A daemon that
    died without cleaning up (SIGKILL, power cut) gets swept here instead.
    """
    deadline = time.time() + timeout
    while True:
        if not ipc.daemon_running() and _files_gone():
            return True
        if time.time() >= deadline:
            break
        time.sleep(0.2)
    if not ipc.daemon_running():
        _cleanup_files()
        return True
    return False


def _files_gone() -> bool:
    """Every runtime file the daemon owns is off disk.

    The pid file is the load-bearing half: Python 3.13's asyncio unlinks the
    unix socket the moment ``Server.close()`` runs, but the daemon only sweeps
    the pid file *after* saving engine state — so a vanished socket no longer
    means a finished daemon.
    """
    return not paths.pid_file().exists() and _endpoint_gone()


def _endpoint_gone() -> bool:
    if ipc.use_tcp():  # pragma: no cover - windows path
        return not ipc.endpoint_file().exists()
    return not paths.socket_path().exists()
