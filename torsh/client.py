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

    async def ensure_connected(self) -> None:
        try:
            await self._to_thread(self.client.get_session)
        except TransmissionError:
            self.reset()
            raise
        except Exception:
            self.reset()
            raise

    async def list_torrents(self) -> List[TorrentView]:
        torrents = await self._to_thread(self.client.get_torrents)
        views: list[TorrentView] = []
        for t in torrents:
            views.append(self._map_torrent(t))
        return views

    async def session_stats(self):
        # Support both get_session_stats (preferred) and older clients without it.
        try:
            return await self._to_thread(self.client.get_session_stats)
        except AttributeError:
            getter = getattr(self.client, "session_stats", None)
            if callable(getter):
                return await self._to_thread(getter)
            return await self._to_thread(self.client.get_session)

    async def add(self, link: str, download_dir: Optional[str] = None) -> Torrent:
        return await self._to_thread(
            self.client.add_torrent,
            link,
            download_dir=download_dir or str(self.config.paths.download_dir),
        )

    async def start(self, ids: Iterable[int]):
        return await self._to_thread(self.client.start_torrent, ids)

    async def stop(self, ids: Iterable[int]):
        return await self._to_thread(self.client.stop_torrent, ids)

    async def remove(self, ids: Iterable[int], delete_data: bool = False):
        return await self._to_thread(self.client.remove_torrent, ids, delete_data=delete_data)

    async def move(self, ids: Iterable[int], location: str, move_data: bool = True):
        return await self._to_thread(self.client.move_torrent_data, ids, location=location, move=move_data)

    async def verify(self, ids: Iterable[int]):
        return await self._to_thread(self.client.verify_torrent, ids)

    async def get_speed_limits(self) -> dict:
        session = await self._to_thread(self.client.get_session)
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
            await self._to_thread(self.client.set_session, **kwargs)

    async def get_files(self, torrent_id: int) -> dict[int, dict]:
        torrent = await self._to_thread(self.client.get_torrent, torrent_id)
        files_attr = getattr(torrent, "files", {})
        files = files_attr() if callable(files_attr) else files_attr  # type: ignore[misc]
        return files or {}

    async def set_priority(self, torrent_id: int, high: list[int], normal: list[int], low: list[int]):
        await self._to_thread(
            self.client.set_torrent,
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
            await self._to_thread(self.client.set_torrent, torrent_id, **kwargs)

    async def get_torrent_speed(self, torrent_id: int) -> dict[str, int]:
        torrent = await self._to_thread(self.client.get_torrent, torrent_id)
        down = getattr(torrent, "download_limit", 0) or 0
        up = getattr(torrent, "upload_limit", 0) or 0
        if getattr(torrent, "download_limited", False) is False:
            down = 0
        if getattr(torrent, "upload_limited", False) is False:
            up = 0
        return {"down": int(down), "up": int(up)}

    async def get_trackers(self, torrent_id: int) -> list[dict]:
        """Get tracker information for a torrent."""
        torrent = await self._to_thread(self.client.get_torrent, torrent_id)
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

        return TorrentView(
            id=t.id,
            name=t.name,
            percent_done=float(t.progress),
            status=str(t.status),
            eta=eta,
            rate_down=humanize.naturalsize(t.rate_download) + "/s",
            rate_up=humanize.naturalsize(t.rate_upload) + "/s",
            ratio=float(t.ratio or 0),
            size=humanize.naturalsize(t.total_size or 0),
            added=t.date_added,
            download_dir=t.download_dir,
            peers=len(t.peers) if t.peers else 0,
            seeders=t.seeders or 0,
            leechers=t.leechers or 0,
        )


