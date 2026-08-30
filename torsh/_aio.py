"""The asyncio JSON-RPC client (TUI side of :mod:`torsh.ipc`).

Lives in its own module so the synchronous CLI never pays the asyncio
import on startup — ``torsh.ipc`` re-exports :class:`AsyncClient` lazily.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from . import paths
from .ipc import (
    _CALL_TIMEOUT,
    _NO_RETRY_AFTER_SEND,
    MAX_MESSAGE,
    DaemonUnavailable,
    RpcError,
    _raise_on_error,
    _SendFailed,
    decode_message,
    encode_request,
    read_tcp_endpoint,
    use_tcp,
)


class AsyncClient:
    """asyncio JSON-RPC client; calls are serialised over one connection."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def connect(self) -> None:
        if use_tcp():  # pragma: no cover - windows path
            try:
                port, token = read_tcp_endpoint()
            except (OSError, ValueError, KeyError) as exc:
                raise DaemonUnavailable("daemon endpoint not found") from exc
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=MAX_MESSAGE)
            except OSError as exc:
                raise DaemonUnavailable(f"daemon not reachable: {exc}") from exc
            writer.write((json.dumps({"auth": token}) + "\n").encode())
            await writer.drain()
        else:
            try:
                reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()), limit=MAX_MESSAGE)
            except OSError as exc:
                raise DaemonUnavailable(f"daemon not reachable: {exc}") from exc
        self._reader, self._writer = reader, writer

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
        self._reader = self._writer = None

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def call(self, method: str, **params: Any) -> Any:
        async with self._lock:
            if self._writer is None:
                await self.connect()
            try:
                return await self._roundtrip(method, params)
            except _SendFailed:
                # Request never reached the daemon: any method is safe to retry.
                await self.close()
                await self.connect()
                return await self._final_attempt(method, params)
            # asyncio.TimeoutError is a distinct type on 3.10 (unified with
            # TimeoutError in 3.11) — missing it would leave the connection
            # open with a stray reply in flight, desyncing every later call.
            except (ConnectionResetError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
                # The request may have EXECUTED and only the reply was lost —
                # re-sending a non-idempotent method would double-execute it.
                await self.close()
                if method in _NO_RETRY_AFTER_SEND:
                    raise RpcError(
                        "connection_lost",
                        f"connection lost waiting for the '{method}' reply",
                    ) from exc
                await self.connect()
                return await self._final_attempt(method, params)

    async def _final_attempt(self, method: str, params: dict[str, Any]) -> Any:
        try:
            return await self._roundtrip(method, params)
        except (_SendFailed, ConnectionResetError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
            await self.close()
            raise DaemonUnavailable(f"daemon connection failed: {exc}") from exc

    async def _roundtrip(self, method: str, params: dict[str, Any]) -> Any:
        assert self._reader is not None and self._writer is not None
        self._next_id += 1
        try:
            self._writer.write(encode_request(self._next_id, method, params))
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise _SendFailed(str(exc)) from exc
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=_CALL_TIMEOUT)
        except asyncio.LimitOverrunError as exc:  # reply line > MAX_MESSAGE
            await self.close()  # the tail of the reply would desync the stream
            raise RpcError("oversized", f"daemon reply exceeded {MAX_MESSAGE // 1024**2} MiB") from exc
        except ValueError as exc:  # StreamReader signals overrun as ValueError too
            await self.close()
            raise RpcError("oversized", str(exc)) from exc
        if not line:
            raise ConnectionResetError("daemon closed the connection")
        return _raise_on_error(decode_message(line))
