"""Client ↔ daemon IPC.

A deliberately tiny protocol: newline-delimited JSON objects over a Unix
domain socket (macOS/Linux) or a token-guarded loopback TCP socket
(Windows, where ``AF_UNIX`` is unavailable to Python).

Request::

    {"id": 1, "method": "list", "params": {}}

Response::

    {"id": 1, "ok": true,  "result": ...}
    {"id": 1, "ok": false, "error": {"code": "not_found", "message": "…"}}

Both a synchronous client (CLI — no event loop, snappy startup) and an
asyncio client (TUI, re-exported lazily from :mod:`torsh._aio` so the CLI
never imports asyncio) are provided, plus :func:`ensure_daemon`, which
transparently spawns the daemon when it is not running yet.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import atomic, log, paths

LOG = log.get(__name__)

MAX_MESSAGE = 64 * 1024 * 1024  # a files() reply for a huge torrent fits
_CONNECT_TIMEOUT = 5.0
_CALL_TIMEOUT = 120.0  # add() may fetch a .torrent over HTTP
_SPAWN_WAIT = 15.0

_IS_WINDOWS = sys.platform == "win32"


class DaemonUnavailable(Exception):
    """The daemon is not running and could not be started."""


class RpcError(Exception):
    """The daemon answered with an error."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


class _SendFailed(Exception):
    """The request never left this process — always safe to retry."""


# Re-sending these after a *lost reply* would double-execute them: a retried
# `add` turns into a spurious "already added", a retried `remove` into
# "not found". Everything else torsh exposes is idempotent.
_NO_RETRY_AFTER_SEND = {"add", "remove"}


def encode_request(request_id: int, method: str, params: dict[str, Any]) -> bytes:
    return (json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode()


def decode_message(line: bytes) -> dict[str, Any]:
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("protocol messages must be JSON objects")
    return message


def _raise_on_error(response: dict[str, Any]) -> Any:
    if response.get("ok"):
        return response.get("result")
    error = response.get("error") or {}
    raise RpcError(
        error.get("code", "error"),
        error.get("message", "daemon error"),
        error.get("data") or {},
    )


# ---------------------------------------------------------------------------
# TCP fallback endpoint (Windows)
# ---------------------------------------------------------------------------


def endpoint_file() -> Path:
    return paths.state_dir() / "daemon.endpoint"


def write_tcp_endpoint(port: int) -> str:
    token = secrets.token_hex(16)
    # Atomic and private from the first byte: a write-then-chmod would leave
    # the auth token world-readable for the width of that window.
    atomic.write_text(endpoint_file(), json.dumps({"port": port, "token": token}))
    return token


def read_tcp_endpoint() -> tuple[int, str]:
    data = json.loads(endpoint_file().read_text(encoding="utf-8"))
    return int(data["port"]), str(data["token"])


def use_tcp() -> bool:
    return _IS_WINDOWS or not hasattr(socket, "AF_UNIX")


# ---------------------------------------------------------------------------
# Synchronous client (CLI)
# ---------------------------------------------------------------------------


class Client:
    """Blocking JSON-RPC client with one persistent connection."""

    def __init__(self, call_timeout: float = _CALL_TIMEOUT) -> None:
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._next_id = 0
        self._call_timeout = call_timeout

    # -- connection -----------------------------------------------------

    def connect(self, timeout: float = _CONNECT_TIMEOUT) -> None:
        if use_tcp():
            try:
                port, token = read_tcp_endpoint()
            except (OSError, ValueError, KeyError) as exc:
                raise DaemonUnavailable("daemon endpoint not found") from exc
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
            except OSError as exc:
                raise DaemonUnavailable(f"daemon not reachable: {exc}") from exc
            self._adopt(sock)
            self._send_raw({"auth": token})
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(str(paths.socket_path()))
            except OSError as exc:
                sock.close()
                raise DaemonUnavailable(f"daemon not reachable: {exc}") from exc
            self._adopt(sock)

    def _adopt(self, sock: socket.socket) -> None:
        sock.settimeout(self._call_timeout)
        self._sock = sock
        self._file = sock.makefile("rb")

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        self._file = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- calls ------------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        if self._sock is None:
            self.connect()
        try:
            return self._roundtrip(method, params)
        except _SendFailed:
            # Request never reached the daemon: any method is safe to retry.
            self.close()
            self.connect()
            return self._final_attempt(method, params)
        except (ConnectionResetError, TimeoutError, OSError) as exc:
            # The request may have EXECUTED and only the reply was lost —
            # re-sending a non-idempotent method would double-execute it.
            self.close()
            if method in _NO_RETRY_AFTER_SEND:
                raise RpcError(
                    "connection_lost",
                    f"connection lost waiting for the '{method}' reply — check `torsh ls` before retrying",
                ) from exc
            self.connect()
            return self._final_attempt(method, params)

    def _final_attempt(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return self._roundtrip(method, params)
        except (_SendFailed, ConnectionResetError, TimeoutError, OSError) as exc:
            self.close()
            raise DaemonUnavailable(f"daemon connection failed: {exc}") from exc

    def _roundtrip(self, method: str, params: dict[str, Any]) -> Any:
        assert self._sock is not None and self._file is not None
        self._next_id += 1
        try:
            self._sock.sendall(encode_request(self._next_id, method, params))
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
            raise _SendFailed(str(exc)) from exc
        line = self._file.readline(MAX_MESSAGE)
        if not line:
            raise ConnectionResetError("daemon closed the connection")
        if not line.endswith(b"\n"):
            # The rest of the giant reply is still in flight — this connection
            # can never realign with request boundaries again. Drop it.
            self.close()
            raise RpcError("oversized", f"daemon reply exceeded {MAX_MESSAGE // 1024**2} MiB")
        return _raise_on_error(decode_message(line))

    def _send_raw(self, payload: dict[str, Any]) -> None:
        assert self._sock is not None
        self._sock.sendall((json.dumps(payload) + "\n").encode())

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Async client (TUI) — lazy re-export so the CLI never imports asyncio
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> Any:
    if name == "AsyncClient":
        from ._aio import AsyncClient

        return AsyncClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Daemon discovery & autostart
# ---------------------------------------------------------------------------


def daemon_running() -> bool:
    """Probe with tight timeouts — a stale socket must not hang callers."""
    client = Client(call_timeout=3.0)
    try:
        client.connect(timeout=1.0)
        client.call("ping")
        return True
    except (DaemonUnavailable, RpcError, OSError, ValueError):
        return False
    finally:
        client.close()


def spawn_daemon() -> subprocess.Popen[bytes]:
    """Start `torsh daemon run` as a detached background process.

    The handle comes back so the caller can notice an immediate death (a
    broken libtorrent install, an unwritable state dir) instead of waiting
    out the whole startup budget for a daemon that will never appear.
    """
    paths.ensure_dirs()
    logfile = open(paths.daemon_log_file(), "ab")  # noqa: SIM115 - handed to the child
    try:
        kwargs: dict[str, Any] = {}
        if _IS_WINDOWS:  # pragma: no cover - windows only
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            [sys.executable, "-m", "torsh", "daemon", "run", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=logfile,
            close_fds=True,
            **kwargs,
        )
    finally:
        logfile.close()


def log_tail(lines: int = 3) -> str:
    """The last few daemon log lines, for pasting into an error message."""
    try:
        content = paths.daemon_log_file().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " / ".join(line.strip() for line in content[-lines:] if line.strip())


def ensure_daemon(*, autostart: bool = True, wait: float = _SPAWN_WAIT) -> Client:
    """Return a connected client, starting the daemon if needed."""
    client = Client()
    try:
        client.connect(timeout=1.5)
        return client
    except DaemonUnavailable:
        if not autostart:
            raise

    LOG.info("daemon not running — starting it")
    process = spawn_daemon()
    deadline = time.time() + wait
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            client.connect(timeout=1.0)
            return client
        except DaemonUnavailable as exc:
            last_error = exc
        exit_code = process.poll()
        if exit_code is not None:
            # It died on startup. Say so now, with its own last words —
            # sitting out the remaining timeout tells the user nothing.
            raise DaemonUnavailable(
                f"daemon exited immediately (status {exit_code})"
                + (f": {tail}" if (tail := log_tail()) else " — check `torsh daemon log`")
            ) from last_error
        time.sleep(0.15)
    raise DaemonUnavailable(
        f"daemon did not come up within {wait:.0f}s — check `torsh daemon log`"
    ) from last_error
