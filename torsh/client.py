"""Async wrapper around ``transmission-rpc``.

All blocking RPC calls are dispatched to a worker thread and wrapped with
bounded retries plus an asyncio timeout, so the UI event loop never stalls on
a slow or flaky daemon. :class:`TorrentView` is a flat, display-ready snapshot
of a torrent — the UI never touches raw RPC objects directly.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, List, Optional

import humanize
from transmission_rpc import Client, Torrent, TransmissionError

from .config import AppConfig
from .logging import get_logger

LOG = get_logger(__name__)


@dataclass(slots=True)
class TorrentView:
    """Flattened, display-ready view of a single torrent."""

    id: int
    name: str
    percent_done: float
    status: str
    eta: str
    rate_down: str
    rate_up: str
    ratio: float
    size: str
    added: datetime | None
    download_dir: str
    peers: int
    seeders: int
    leechers: int
    error: bool = False
    error_string: str = ""


class TransmissionController:
    """Thin async facade over a :class:`transmission_rpc.Client`.

    The client is created lazily and rebuilt automatically whenever a call
    fails, so a daemon restart transparently recovers on the next request.
    """

    def __init__(self, config: AppConfig, *, retries: int = 2, backoff: float = 0.6) -> None:
        self.config = config
        self._client: Client | None = None
        self._default_retries = max(0, retries)
        self._default_delay = max(0.1, backoff)

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(
                host=self.config.rpc.host,
                port=self.config.rpc.port,
                username=self.config.rpc.username,
                password=self.config.rpc.password,
                timeout=self.config.rpc.timeout,
            )
        return self._client

    def reset(self) -> None:
        """Drop the cached client so the next call reconnects from scratch."""
        self._client = None

    # ------------------------------------------------------------------
    # Low-level RPC plumbing
    # ------------------------------------------------------------------

    async def _rpc(self, method_name: str, *args: Any, retries: int | None = None, **kwargs: Any) -> Any:
        """Call a Transmission RPC method with bounded retries and backoff."""
        attempts = (self._default_retries if retries is None else retries) + 1
        delay = self._default_delay
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                method = getattr(self.client, method_name)
                return await asyncio.to_thread(method, *args, **kwargs)
            except (KeyboardInterrupt, SystemExit):
                # Re-raise graceful shutdown signals immediately.
                raise
            except Exception as exc:  # network / timeout / RPC error
                last_error = exc
                self.reset()
                LOG.debug("RPC %s failed (%d/%d): %s", method_name, attempt, attempts, exc)
                if attempt < attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.6, 5.0)

        raise last_error or TransmissionError("Unknown RPC failure")

    async def _call(
        self,
        method_name: str,
        *args: Any,
        retries: int | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """:meth:`_rpc` guarded by an overall asyncio timeout."""
        timeout = timeout or self.config.rpc.timeout
        return await asyncio.wait_for(
            self._rpc(method_name, *args, retries=retries, **kwargs), timeout=timeout
        )

    # ------------------------------------------------------------------
    # Connection & listing
    # ------------------------------------------------------------------

    async def ensure_connected(self) -> None:
        """Raise if the daemon is unreachable; cheap enough to poll with."""
        await self._call("get_session", retries=1)

    async def list_torrents(self) -> List[TorrentView]:
        torrents = await self._call("get_torrents")
        return [self._map_torrent(t) for t in torrents]

    async def session_stats(self) -> Any:
        return await self._call("session_stats")

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add(self, link: str, download_dir: Optional[str] = None) -> Torrent:
        return await self._call(
            "add_torrent",
            link,
            download_dir=download_dir or str(self.config.paths.download_dir),
            paused=False,  # start immediately, matching Transmission's default
        )

    async def start(self, ids: Iterable[int]) -> Any:
        # bypass_queue forces an immediate start, like Transmission's "Resume Now".
        return await self._call("start_torrent", list(ids), bypass_queue=True)

    async def stop(self, ids: Iterable[int]) -> Any:
        return await self._call("stop_torrent", list(ids))

    async def remove(self, ids: Iterable[int], delete_data: bool = False) -> Any:
        return await self._call("remove_torrent", list(ids), delete_data=delete_data)

    async def move(self, ids: Iterable[int], location: str, move_data: bool = True) -> Any:
        return await self._call("move_torrent_data", list(ids), location=location, move=move_data)

    async def verify(self, ids: Iterable[int]) -> Any:
        return await self._call("verify_torrent", list(ids))

    # ------------------------------------------------------------------
    # Speed limits (values are KiB/s, matching the Transmission RPC spec)
    # ------------------------------------------------------------------

    async def get_speed_limits(self) -> dict[str, int]:
        session = await self._call("get_session")
        down = session.speed_limit_down if session.speed_limit_down_enabled else 0
        up = session.speed_limit_up if session.speed_limit_up_enabled else 0
        return {"down": int(down), "up": int(up)}

    async def set_speed_limits(self, down_kib: int | None, up_kib: int | None) -> None:
        kwargs: dict[str, Any] = {}
        if down_kib is not None:
            kwargs["speed_limit_down_enabled"] = down_kib > 0
            kwargs["speed_limit_down"] = max(0, down_kib)
        if up_kib is not None:
            kwargs["speed_limit_up_enabled"] = up_kib > 0
            kwargs["speed_limit_up"] = max(0, up_kib)
        if kwargs:
            await self._call("set_session", **kwargs)

    async def get_torrent_speed(self, torrent_id: int) -> dict[str, int]:
        torrent = await self._call("get_torrent", torrent_id)
        down = int(getattr(torrent, "download_limit", 0) or 0)
        up = int(getattr(torrent, "upload_limit", 0) or 0)
        if not getattr(torrent, "download_limited", False):
            down = 0
        if not getattr(torrent, "upload_limited", False):
            up = 0
        return {"down": down, "up": up}

    async def set_torrent_speed(self, torrent_id: int, down_kib: int | None, up_kib: int | None) -> None:
        kwargs: dict[str, Any] = {}
        if down_kib is not None:
            kwargs["download_limit"] = max(0, down_kib)
            kwargs["download_limited"] = down_kib > 0
        if up_kib is not None:
            kwargs["upload_limit"] = max(0, up_kib)
            kwargs["upload_limited"] = up_kib > 0
        if kwargs:
            await self._call("change_torrent", torrent_id, **kwargs)

    # ------------------------------------------------------------------
    # Files & priorities
    # ------------------------------------------------------------------

    async def get_files(self, torrent_id: int) -> dict[int, dict[str, Any]]:
        """Return files keyed by their index, normalised for the UI layer."""
        torrent = await self._call("get_torrent", torrent_id)
        get_files = getattr(torrent, "get_files", None)
        raw_files = get_files() if callable(get_files) else getattr(torrent, "files", []) or []

        result: dict[int, dict[str, Any]] = {}
        for idx, f in enumerate(raw_files):
            file_id = self._as_int(getattr(f, "id", idx))
            result[file_id] = {
                "name": getattr(f, "name", "Unknown"),
                "length": self._as_int(getattr(f, "size", getattr(f, "length", 0))),
                "bytesCompleted": self._as_int(getattr(f, "completed", getattr(f, "bytesCompleted", 0))),
                "priority": self._as_int(getattr(f, "priority", 0)),
            }
        return result

    async def set_priority(
        self, torrent_id: int, high: list[int], normal: list[int], low: list[int]
    ) -> None:
        if not (high or normal or low):
            return  # nothing selected; change_torrent rejects an empty payload
        await self._call(
            "change_torrent",
            torrent_id,
            priority_high=high or None,
            priority_normal=normal or None,
            priority_low=low or None,
        )

    # ------------------------------------------------------------------
    # Trackers
    # ------------------------------------------------------------------

    async def get_trackers(self, torrent_id: int) -> list[dict[str, Any]]:
        """Return tracker stats for a torrent in a UI-friendly shape."""
        torrent = await self._call("get_torrent", torrent_id)
        trackers = getattr(torrent, "tracker_stats", None) or getattr(torrent, "trackers", []) or []

        result: list[dict[str, Any]] = []
        for t in trackers:
            if isinstance(t, dict):
                result.append({
                    "host": t.get("host", t.get("announce", "unknown")),
                    "status": t.get("lastAnnounceResult", "") or "",
                    "peers": t.get("lastAnnouncePeerCount", 0),
                    "seeders": t.get("seederCount", 0),
                    "leechers": t.get("leecherCount", 0),
                })
            else:
                result.append({
                    "host": getattr(t, "host", getattr(t, "announce", "unknown")),
                    "status": getattr(t, "last_announce_result", "") or "",
                    "peers": self._as_int(getattr(t, "last_announce_peer_count", 0)),
                    "seeders": self._as_int(getattr(t, "seeder_count", 0)),
                    "leechers": self._as_int(getattr(t, "leecher_count", 0)),
                })
        return result

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _map_torrent(self, t: Torrent) -> TorrentView:
        return TorrentView(
            id=t.id,
            name=t.name,
            percent_done=self._percent_done(t),
            status=str(getattr(t, "status", "unknown")),
            eta=self._format_eta(getattr(t, "eta", None)),
            rate_down=self._natural_rate(getattr(t, "rate_download", 0)),
            rate_up=self._natural_rate(getattr(t, "rate_upload", 0)),
            ratio=self._as_float(getattr(t, "ratio", 0.0)) or 0.0,
            size=humanize.naturalsize(getattr(t, "total_size", 0) or 0, binary=True),
            added=getattr(t, "added_date", None),
            download_dir=getattr(t, "download_dir", ""),
            peers=self._as_int(getattr(t, "peers_connected", 0)),
            seeders=self._as_int(getattr(t, "peers_sending_to_us", 0)),
            leechers=self._as_int(getattr(t, "peers_getting_from_us", 0)),
            error=self._as_int(getattr(t, "error", 0)) != 0,
            error_string=str(getattr(t, "error_string", "") or ""),
        )

    @staticmethod
    def _format_eta(eta: timedelta | int | None) -> str:
        """Render the ETA, tolerating both ``timedelta`` and raw-second forms."""
        if eta is None:
            return "—"
        if isinstance(eta, timedelta):
            return humanize.naturaldelta(eta) if eta.total_seconds() > 0 else "—"
        # Older clients return raw seconds: -1 (unavailable) / -2 (unknown).
        if eta > 0:
            return humanize.naturaldelta(timedelta(seconds=eta))
        return "∞" if eta < -1 else "—"

    @classmethod
    def _percent_done(cls, t: Torrent) -> float:
        """Resolve completion as a 0–100 float across client versions."""
        raw = cls._as_float(getattr(t, "percent_done", None))
        if raw is not None:
            return max(0.0, min(100.0, raw * 100.0))

        size = cls._as_float(getattr(t, "size_when_done", None))
        left = cls._as_float(getattr(t, "left_until_done", None))
        if size and size > 0 and left is not None:
            return max(0.0, min(100.0, (size - left) / size * 100.0))

        progress = cls._as_float(getattr(t, "progress", None)) or 0.0
        return max(0.0, min(100.0, progress))

    @staticmethod
    def _natural_rate(value: Any) -> str:
        try:
            clean = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            clean = 0.0
        return humanize.naturalsize(clean, binary=True) + "/s"

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
