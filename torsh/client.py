import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

import humanize
from transmission_rpc import Client, TransmissionError, Torrent

from .config import AppConfig
from .logging import get_logger


LOG = get_logger(__name__)


@dataclass
class TorrentView:
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


class TransmissionController:
    def __init__(self, config: AppConfig):
        self.config = config
        self._client: Client | None = None
        self._default_retries = 2
        self._default_delay = 0.6

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
        self._client = None

    async def _to_thread(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _rpc(self, method_name: str, *args, retries: int | None = None, **kwargs):
        """Call Transmission RPC with small retry/backoff for resilience."""
        attempts = (self._default_retries if retries is None else retries) + 1
        delay = self._default_delay
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                method = getattr(self.client, method_name)
                return await self._to_thread(method, *args, **kwargs)
            except TransmissionError as exc:
                last_error = exc
                self.reset()
            except Exception as exc:  # network/timeouts/etc
                last_error = exc
                self.reset()
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)
        if last_error:
            raise last_error
        raise TransmissionError("Unknown RPC failure")

    async def ensure_connected(self) -> None:
        await self._rpc("get_session", retries=1)

    async def list_torrents(self) -> List[TorrentView]:
        torrents = await self._rpc("get_torrents")
        views: list[TorrentView] = []
        for t in torrents:
            views.append(self._map_torrent(t))
        return views

    async def session_stats(self):
        # Support both get_session_stats (preferred) and older clients without it.
        try:
            return await self._rpc("get_session_stats")
        except AttributeError:
            getter = getattr(self.client, "session_stats", None)
            if callable(getter):
                return await self._rpc("session_stats")
            return await self._rpc("get_session")

    async def add(self, link: str, download_dir: Optional[str] = None) -> Torrent:
        return await self._rpc(
            "add_torrent",
            link,
            download_dir=download_dir or str(self.config.paths.download_dir),
            paused=False,  # сразу стартуем как в Transmission по умолчанию
        )

    async def start(self, ids: Iterable[int]):
        # bypass_queue=True чтобы форсировать старт, как в Transmission "Resume Now"
        return await self._rpc("start_torrent", ids, bypass_queue=True)

    async def stop(self, ids: Iterable[int]):
        return await self._rpc("stop_torrent", ids)

    async def remove(self, ids: Iterable[int], delete_data: bool = False):
        return await self._rpc("remove_torrent", ids, delete_data=delete_data)

    async def move(self, ids: Iterable[int], location: str, move_data: bool = True):
        return await self._rpc("move_torrent_data", ids, location=location, move=move_data)

    async def verify(self, ids: Iterable[int]):
        return await self._rpc("verify_torrent", ids)

    async def get_speed_limits(self) -> dict:
        session = await self._rpc("get_session")
        return {
            "down": session.download_speed_limit if session.speed_limit_down_enabled else 0,
            "up": session.upload_speed_limit if session.speed_limit_up_enabled else 0,
        }

    async def set_speed_limits(self, down_kib: int | None, up_kib: int | None):
        kwargs = {}
        if down_kib is not None:
            kwargs["speed_limit_down_enabled"] = down_kib > 0
            kwargs["download_speed_limit"] = max(0, down_kib)
        if up_kib is not None:
            kwargs["speed_limit_up_enabled"] = up_kib > 0
            kwargs["upload_speed_limit"] = max(0, up_kib)
        if kwargs:
            await self._rpc("set_session", **kwargs)

    async def get_files(self, torrent_id: int) -> dict[int, dict]:
        torrent = await self._rpc("get_torrent", torrent_id)
        files_attr = getattr(torrent, "files", {})
        files = files_attr() if callable(files_attr) else files_attr  # type: ignore[misc]
        return files or {}

    async def set_priority(self, torrent_id: int, high: list[int], normal: list[int], low: list[int]):
        await self._rpc(
            "set_torrent",
            torrent_id,
            priority_high=high or None,
            priority_normal=normal or None,
            priority_low=low or None,
        )

    async def set_torrent_speed(self, torrent_id: int, down_kib: int | None, up_kib: int | None):
        kwargs = {}
        if down_kib is not None:
            kwargs["downloadLimit"] = max(0, down_kib)
            kwargs["downloadLimited"] = down_kib > 0
        if up_kib is not None:
            kwargs["uploadLimit"] = max(0, up_kib)
            kwargs["uploadLimited"] = up_kib > 0
        if kwargs:
            await self._rpc("set_torrent", torrent_id, **kwargs)

    async def get_torrent_speed(self, torrent_id: int) -> dict[str, int]:
        torrent = await self._rpc("get_torrent", torrent_id)
        down = getattr(torrent, "download_limit", 0) or 0
        up = getattr(torrent, "upload_limit", 0) or 0
        if getattr(torrent, "download_limited", False) is False:
            down = 0
        if getattr(torrent, "upload_limited", False) is False:
            up = 0
        return {"down": int(down), "up": int(up)}

    async def get_trackers(self, torrent_id: int) -> list[dict]:
        """Get tracker information for a torrent."""
        torrent = await self._rpc("get_torrent", torrent_id)
        trackers = getattr(torrent, "tracker_stats", None)
        if trackers is None:
            trackers = getattr(torrent, "trackers", [])
        
        result = []
        for t in trackers or []:
            if hasattr(t, "__dict__"):
                result.append({
                    "host": getattr(t, "host", getattr(t, "announce", "unknown")),
                    "status": getattr(t, "last_announce_result", "unknown"),
                    "peers": getattr(t, "last_announce_peer_count", 0),
                    "seeders": getattr(t, "seeder_count", 0),
                    "leechers": getattr(t, "leecher_count", 0),
                })
            elif isinstance(t, dict):
                result.append({
                    "host": t.get("host", t.get("announce", "unknown")),
                    "status": t.get("lastAnnounceResult", "unknown"),
                    "peers": t.get("lastAnnouncePeerCount", 0),
                    "seeders": t.get("seederCount", 0),
                    "leechers": t.get("leecherCount", 0),
                })
        return result

    def _map_torrent(self, t: Torrent) -> TorrentView:
        eta = "—"
        if t.eta and t.eta > 0:
            eta = humanize.naturaldelta(t.eta)
        elif t.eta and t.eta < 0:
            eta = "∞"

        raw_percent = getattr(t, "percentDone", None)
        if raw_percent is None:
            raw_percent = getattr(t, "progress", 0) or 0
        # Если есть точные размеры, пересчитаем процент сами для достоверности
        size_when_done = getattr(t, "sizeWhenDone", None) or getattr(t, "size_when_done", None)
        left_until_done = getattr(t, "leftUntilDone", None) or getattr(t, "left_until_done", None)
        if size_when_done and size_when_done > 0 and left_until_done is not None:
            calc_percent = (size_when_done - left_until_done) / size_when_done * 100.0
            percent_done = max(0.0, min(100.0, float(calc_percent)))
        else:
            percent_done = float(raw_percent * 100.0) if raw_percent <= 1.0 else float(raw_percent)

        rate_down = (
            getattr(t, "rate_download", None)
            or getattr(t, "rateDownload", None)
            or 0
        )
        rate_up = (
            getattr(t, "rate_upload", None)
            or getattr(t, "rateUpload", None)
            or 0
        )

        peers_connected = (
            getattr(t, "peers_connected", None)
            or getattr(t, "peersConnected", None)
            or 0
        )
        seeders = (
            getattr(t, "peers_sending_to_us", None)
            or getattr(t, "peersSendingToUs", None)
            or 0
        )
        leechers = (
            getattr(t, "peers_getting_from_us", None)
            or getattr(t, "peersGettingFromUs", None)
            or 0
        )

        return TorrentView(
            id=t.id,
            name=t.name,
            percent_done=percent_done,
            status=str(t.status),
            eta=eta,
            rate_down=humanize.naturalsize(rate_down) + "/s",
            rate_up=humanize.naturalsize(rate_up) + "/s",
            ratio=float(t.ratio or 0),
            size=humanize.naturalsize(t.total_size or 0),
            added=t.added_date,
            download_dir=t.download_dir,
            peers=peers_connected,
            seeders=seeders,
            leechers=leechers,
        )


