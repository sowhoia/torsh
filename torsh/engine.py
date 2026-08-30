"""Native BitTorrent engine.

torsh 1.0 replaces the external Transmission daemon with an embedded
libtorrent 2.0 session — the same engine qBittorrent and Deluge build on.
Nothing to install, no RPC ports to juggle, and every capability (sequential
streaming, per-file priorities, magnet metadata, DHT) is first-class.

Threading model
---------------
The :class:`Engine` is synchronous and thread-safe: a single internal alert
thread pumps libtorrent alerts and keeps a status cache fresh via
``post_torrent_updates``; public methods take a lock only around torsh's own
bookkeeping (libtorrent's session object is itself thread-safe).  The daemon
calls public methods from worker threads, so a slow disk or tracker never
stalls the RPC loop.

Persistence
-----------
Fast-resume blobs (with the info dict embedded, so magnets survive restarts)
live in ``<state>/session/resume/<infohash>.resume``; DHT/session state in
``state.dat``; the human-friendly integer ids in ``ids.json``.
"""

from __future__ import annotations

import binascii
import json
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import libtorrent as lt

from . import atomic, log, paths
from .config import Config

LOG = log.get(__name__)

# File priority vocabulary (libtorrent values).
PRIORITY_NAMES = {"skip": 0, "low": 1, "normal": 4, "high": 7}
PRIORITY_LABELS = {0: "skip", 1: "low", 4: "normal", 7: "high"}

_TORRENT_URL_MAX_BYTES = 64 * 1024 * 1024
_STOP_SAVE_TIMEOUT = 10.0

# Where a torrent can be moved inside the queue (`torsh queue`).
QUEUE_MOVES = ("up", "down", "top", "bottom")

# Well-known DHT entry points. Several, from independent operators, because a
# magnet link with no working bootstrap node simply never resolves.
_DHT_BOOTSTRAP_NODES = (
    "dht.libtorrent.org:25401",
    "router.bittorrent.com:6881",
    "router.utorrent.com:6881",
    "dht.transmissionbt.com:6881",
)

_lt_states = lt.torrent_status.states


class EngineError(Exception):
    """A user-facing engine failure with a machine-readable code.

    ``data`` carries structured context (e.g. the colliding infohash for
    ``duplicate``) so frontends never have to parse the message text.
    """

    def __init__(self, message: str, *, code: str = "engine", data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def _norm_priority(value: int | str) -> int:
    if isinstance(value, str):
        try:
            return PRIORITY_NAMES[value.lower()]
        except KeyError:
            raise EngineError(f"unknown priority {value!r} (skip/low/normal/high)", code="invalid") from None
    if value in PRIORITY_LABELS:
        return int(value)
    raise EngineError(f"priority must be one of 0/1/4/7, got {value!r}", code="invalid")


def _hash_str(info_hashes: Any) -> str:
    return str(info_hashes.get_best())


def _alert_hash(alert: Any) -> str:
    """Infohash of a save-resume alert (success carries params, failure a handle)."""
    try:
        params = getattr(alert, "params", None)
        if params is not None:
            return _hash_str(params.info_hashes)
        return _hash_str(alert.handle.info_hashes())
    except Exception:  # noqa: BLE001 - shutdown path, stay forgiving
        return ""


def _ratio_of(st: lt.torrent_status) -> float:
    """Share ratio with a fallback denominator for seeded-from-disk data."""
    denominator = st.all_time_download or st.total_done
    return (st.all_time_upload / denominator) if denominator else 0.0


def _mkdir(path: Path, *, what: str) -> None:
    """Create a directory, reporting failures as a user-facing engine error.

    Unwrapped ``OSError``s here would surface as an opaque ``internal`` RPC
    error plus a traceback in the daemon log; a read-only or full disk is a
    perfectly ordinary thing for a user to hit.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EngineError(f"can't use {what} {path}: {exc.strerror or exc}", code="io") from exc


def _safe_filename(name: str) -> str:
    """A torrent name is attacker-controlled; never let it escape a directory."""
    cleaned = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in name).strip(" .")
    return (cleaned or "torrent")[:120]


_BASE32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def _looks_like_hash(text: str) -> bool:
    """Is this a bare infohash — 40 hex chars (v1/v2) or 32 base32 (v1)?

    Deliberately strict: ``str.isalnum()`` would happily accept ``"ＵＢＵＮＴＵ…"``
    or any 32-character Cyrillic word and send it off to be parsed as a magnet
    link, turning a plain typo into a confusing "not a valid torrent".
    """
    text = text.strip()
    if len(text) == 40:
        try:
            binascii.unhexlify(text)
            return True
        except (binascii.Error, ValueError):
            return False
    return len(text) == 32 and _BASE32_ALPHABET.issuperset(text.upper())


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Keep a ``.torrent`` fetch on http(s) all the way to the last hop."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not newurl.lower().startswith(("http://", "https://")):
            raise urllib.error.HTTPError(newurl, code, f"refusing redirect to {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Engine:
    """Embedded libtorrent session with torsh's persistence and id scheme."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: lt.session | None = None
        self._lock = threading.RLock()
        # Serialises writers of ids.json; always taken *before* _lock.
        self._persist_lock = threading.Lock()
        self._handles: dict[str, lt.torrent_handle] = {}
        self._status: dict[str, lt.torrent_status] = {}
        self._ids: dict[str, int] = {}
        self._next_id = 1
        # False until ids.json has been read: _save_ids refuses to write over
        # a map it never loaded.
        self._ids_loaded = False
        self._ratio_handled: set[str] = set()
        self._counters: dict[str, int] = {}
        self._alert_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = 0.0
        self._add_failures = 0  # async resume-adds rejected by libtorrent
        self._dispatch: dict[type, Callable[[Any], None]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._session is not None:
            raise EngineError("engine is already running", code="running")
        paths.ensure_dirs()
        self._reset_state()
        # The Python bindings accept a settings dict; the stubs only
        # describe the C++ settings_pack overloads.
        self._session = lt.session(self._settings_pack())  # type: ignore[call-overload]
        try:
            self._load_session_state()
            self._load_ids()
            self._dispatch = {
                lt.add_torrent_alert: self._on_add_torrent,
                lt.state_update_alert: self._on_state_update,
                lt.session_stats_alert: self._on_session_stats,
                lt.save_resume_data_alert: self._on_save_resume,
                lt.save_resume_data_failed_alert: self._on_save_resume_failed,
                lt.metadata_received_alert: self._on_metadata,
                lt.torrent_finished_alert: self._on_finished,
            }
            issued = self._load_resume_files()
            self._started_at = time.time()
            self._stop_event.clear()
            self._alert_thread = threading.Thread(target=self._alert_loop, name="torsh-alerts", daemon=True)
            self._alert_thread.start()
            self._wait_for_registration(issued)
        except BaseException:
            # A half-started engine is worse than none: the caller sees the
            # failure but the alert thread would keep pumping a session
            # nothing owns, and stop() would never be reached to join it.
            self.stop()
            raise
        LOG.info("engine started: %d torrent(s), listen port %s", len(self._handles), self.listen_port())

    def _reset_state(self) -> None:
        """Forget the previous session, so start-after-stop is a clean slate."""
        with self._lock:
            self._handles.clear()
            self._status.clear()
            self._ids.clear()
            self._ratio_handled.clear()
            self._counters.clear()
            self._next_id = 1
            self._ids_loaded = False
            self._add_failures = 0
            self._started_at = 0.0

    def stop(self) -> None:
        session = self._session
        if session is None:
            return
        self._stop_event.set()
        pump_stopped = True
        if self._alert_thread is not None:
            self._alert_thread.join(timeout=5.0)
            # A wedged pump (e.g. resume write stuck on a dying disk) must not
            # leave two threads calling pop_alerts(): each pop invalidates the
            # previous batch, so the other thread would iterate freed memory.
            pump_stopped = not self._alert_thread.is_alive()
            self._alert_thread = None

        session.pause()
        pending: set[str] = set()
        with self._lock:
            handles = list(self._handles.items())
        for info_hash, handle in handles:
            try:
                if handle.is_valid() and handle.status().has_metadata:
                    handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                    pending.add(info_hash)
            except Exception:  # noqa: BLE001 - a dying handle must not block shutdown
                continue

        deadline = time.time() + _STOP_SAVE_TIMEOUT
        while pump_stopped and pending and time.time() < deadline:
            session.wait_for_alert(250)
            for alert in session.pop_alerts():
                # Match by infohash: alerts from earlier periodic saves must
                # not be double-counted against the shutdown sweep.
                if isinstance(alert, (lt.save_resume_data_alert, lt.save_resume_data_failed_alert)):
                    pending.discard(_alert_hash(alert))
                self._handle_alert(alert)
        if pending:
            LOG.warning("shutdown resume sweep incomplete for %d torrent(s)", len(pending))

        self._save_session_state()
        self._save_ids()
        self._session = None
        # A stopped engine owns nothing: leaving the caches populated would
        # have torrents() answer for handles whose session is already gone.
        self._reset_state()
        LOG.info("engine stopped")

    def _settings_pack(self) -> dict[str, Any]:
        from . import __version__

        net, limits = self.config.network, self.config.limits
        port = net.port or 0
        enc = {
            "require": lt.enc_policy.forced,
            "prefer": lt.enc_policy.enabled,
            "allow": lt.enc_policy.enabled,
        }.get(net.encryption, lt.enc_policy.enabled)

        return {
            "user_agent": f"torsh/{__version__}",
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "alert_mask": (
                lt.alert.category_t.error_notification
                | lt.alert.category_t.status_notification
                | lt.alert.category_t.storage_notification
            ),
            "enable_dht": net.dht,
            "enable_upnp": net.upnp,
            "enable_natpmp": net.natpmp,
            "enable_lsd": net.lsd,
            "enable_outgoing_utp": net.utp,
            "enable_incoming_utp": net.utp,
            "out_enc_policy": int(enc),
            "in_enc_policy": int(enc),
            "connections_limit": net.max_connections,
            "download_rate_limit": limits.download,
            "upload_rate_limit": limits.upload,
            "active_downloads": limits.active_downloads,
            "active_seeds": limits.active_seeds,
            "active_limit": limits.active_downloads + limits.active_seeds + 4,
            "alert_queue_size": 20_000,
            "dht_bootstrap_nodes": ",".join(_DHT_BOOTSTRAP_NODES),
        }

    def apply_config(self, config: Config) -> None:
        """Apply a freshly loaded config to the live session."""
        with self._lock:
            self.config = config
        session = self._require_session()
        pack = self._settings_pack()
        # listen_interfaces changes would re-bind sockets mid-flight; skip it.
        pack.pop("listen_interfaces", None)
        session.apply_settings(pack)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Adding torrents
    # ------------------------------------------------------------------

    def add(
        self,
        uri: str,
        *,
        download_dir: str | None = None,
        paused: bool = False,
        sequential: bool | None = None,
    ) -> dict[str, Any]:
        """Add a magnet link, ``.torrent`` path/URL or raw infohash."""
        session = self._require_session()
        atp = self._build_params(uri.strip())

        save_dir = Path(download_dir).expanduser() if download_dir else self.config.downloads.resolved_dir()
        if not save_dir.is_absolute():
            # Same rule as move(): a relative path would silently resolve
            # against the daemon's cwd, scattering data out of sight.
            raise EngineError(f"download dir must be absolute: {save_dir}", code="invalid")
        _mkdir(save_dir, what="download dir")
        atp.save_path = str(save_dir)

        seq = self.config.downloads.sequential if sequential is None else sequential
        atp.flags |= lt.torrent_flags.duplicate_is_error
        if seq:
            atp.flags |= lt.torrent_flags.sequential_download
        if paused or self.config.downloads.start_paused:
            atp.flags |= lt.torrent_flags.paused
            atp.flags &= ~lt.torrent_flags.auto_managed
        else:
            atp.flags |= lt.torrent_flags.auto_managed
            atp.flags &= ~lt.torrent_flags.paused

        info_hash = _hash_str(atp.info_hashes) if atp.ti is None else _hash_str(atp.ti.info_hashes())
        with self._lock:
            if info_hash in self._handles:
                existing = self._status.get(info_hash)
                # Same name fallback as _view(), so messages stay consistent.
                name = (existing.name if existing is not None else "") or info_hash[:12]
                raise EngineError(f"already added: {name}", code="duplicate", data={"hash": info_hash})

        try:
            handle = session.add_torrent(atp)
        except RuntimeError as exc:
            with self._lock:
                lost_race = info_hash in self._handles
            if lost_race:
                # Two concurrent adds of the same torrent: the loser must get
                # the same structured "duplicate" error as the sequential case.
                raise EngineError(
                    f"already added: {info_hash[:12]}", code="duplicate", data={"hash": info_hash}
                ) from exc
            raise EngineError(f"libtorrent rejected the torrent: {exc}", code="invalid") from exc

        self._register(handle)
        handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        view = self._view(handle.status())
        LOG.info("added torrent %s (%s)", view["name"], info_hash[:12])
        return view

    def _build_params(self, uri: str) -> lt.add_torrent_params:
        try:
            if uri.startswith("magnet:"):
                return lt.parse_magnet_uri(uri)
            if uri.startswith(("http://", "https://")):
                return lt.load_torrent_buffer(self._fetch_url(uri))
            if _looks_like_hash(uri):
                return lt.parse_magnet_uri(f"magnet:?xt=urn:btih:{uri.strip().lower()}")
            path = Path(uri).expanduser()
            if path.exists():
                return lt.load_torrent_file(str(path))
        except EngineError:
            raise
        except RuntimeError as exc:  # libtorrent parse errors
            raise EngineError(f"not a valid torrent: {exc}", code="invalid") from exc
        raise EngineError(
            f"can't make sense of {uri!r} — pass a magnet link, .torrent file/URL or infohash",
            code="invalid",
        )

    @staticmethod
    def _fetch_url(url: str) -> bytes:
        if not url.startswith(("http://", "https://")):
            # _build_params guarantees this; keep the invariant local anyway
            # so no future caller can smuggle in file:// or custom schemes.
            raise EngineError(f"unsupported URL scheme: {url}", code="invalid")
        request = urllib.request.Request(url, headers={"User-Agent": "torsh"})  # noqa: S310 - scheme checked above
        # A checked scheme is worth nothing if a redirect can change it:
        # urllib's default handler follows 3xx into ftp:// happily.
        opener = urllib.request.build_opener(_HttpOnlyRedirects)
        try:
            with opener.open(request, timeout=30) as response:
                data = response.read(_TORRENT_URL_MAX_BYTES + 1)
        except OSError as exc:
            raise EngineError(f"couldn't fetch {url}: {exc}", code="io") from exc
        if len(data) > _TORRENT_URL_MAX_BYTES:
            cap = _TORRENT_URL_MAX_BYTES // 1024**2
            raise EngineError(f"{url} is larger than {cap} MiB — not a .torrent file", code="invalid")
        return data

    # ------------------------------------------------------------------
    # Torrent operations (hashes are 40-char hex strings)
    # ------------------------------------------------------------------

    def pause(self, hashes: Iterable[str]) -> int:
        count = 0
        for pair in self._resolve_pairs(hashes):
            pair[1].unset_flags(lt.torrent_flags.auto_managed)
            pair[1].pause()
            self._refresh_status(*pair)
            count += 1
        return count

    def resume(self, hashes: Iterable[str]) -> int:
        count = 0
        for info_hash, handle in self._resolve_pairs(hashes):
            with self._lock:
                self._ratio_handled.discard(info_hash)
            handle.set_flags(lt.torrent_flags.auto_managed)
            handle.resume()
            self._refresh_status(info_hash, handle)
            count += 1
        return count

    def recheck(self, hashes: Iterable[str]) -> int:
        count = 0
        for pair in self._resolve_pairs(hashes):
            pair[1].force_recheck()
            self._refresh_status(*pair)
            count += 1
        return count

    def reannounce(self, hashes: Iterable[str]) -> int:
        count = 0
        for handle in self._resolve_handles(hashes):
            handle.force_reannounce()
            handle.force_dht_announce()
            count += 1
        return count

    def remove(self, hashes: Iterable[str], *, delete_data: bool = False) -> int:
        session = self._require_session()
        count = 0
        for info_hash, handle in self._resolve_pairs(hashes):
            flags = lt.session.delete_files if delete_data else 0
            session.remove_torrent(handle, flags)
            with self._lock:
                self._handles.pop(info_hash, None)
                self._status.pop(info_hash, None)
                self._ids.pop(info_hash, None)
                self._ratio_handled.discard(info_hash)
            # Dropping the handle first is what makes this safe: a resume save
            # already in flight re-checks registration after its rename and
            # sweeps the file it just recreated (see _on_save_resume).
            self._resume_file(info_hash).unlink(missing_ok=True)
            count += 1
        self._save_ids()
        return count

    def move(self, info_hash: str, new_dir: str) -> None:
        handle = self._handle_for(info_hash)
        target = Path(new_dir).expanduser()
        if not target.is_absolute():
            # A relative path would silently resolve against the *daemon's*
            # cwd, scattering data somewhere the user never sees.
            raise EngineError(f"path must be absolute: {new_dir}", code="invalid")
        _mkdir(target, what="target dir")
        handle.move_storage(str(target), lt.move_flags_t.dont_replace)
        self._refresh_status(info_hash, handle)

    def set_queue_position(self, info_hash: str, move: str) -> int:
        """Reorder a torrent within libtorrent's download queue."""
        handle = self._handle_for(info_hash)
        action = {
            "up": handle.queue_position_up,
            "down": handle.queue_position_down,
            "top": handle.queue_position_top,
            "bottom": handle.queue_position_bottom,
        }.get(move)
        if action is None:
            raise EngineError(f"unknown queue move {move!r} ({'/'.join(QUEUE_MOVES)})", code="invalid")
        status = handle.status()
        if status.queue_position < 0:
            # libtorrent drops finished torrents out of the queue entirely, so
            # the move would be a silent no-op reporting a nonsense position.
            raise EngineError(
                f"{status.name or info_hash[:12]} is not in the download queue — it has finished",
                code="invalid",
            )
        action()
        self._refresh_status(info_hash, handle)
        return int(handle.status().queue_position)

    def export(self, info_hash: str, target: str) -> str:
        """Write a torrent's metadata back out as a ``.torrent`` file.

        Magnet-added torrents become shareable files once their metadata has
        arrived — the whole reason torsh keeps the info dict in resume data.
        *target* is a directory or an explicit ``.torrent`` path.
        """
        handle = self._handle_for(info_hash)
        status = handle.status()
        ti = handle.torrent_file()
        if ti is None or not status.has_metadata:
            raise EngineError("metadata not downloaded yet", code="pending")

        path = Path(target).expanduser()
        if not path.is_absolute():
            # Same rule as add()/move(): the daemon's cwd is not the user's.
            raise EngineError(f"path must be absolute: {target}", code="invalid")
        if path.is_dir() or not path.suffix:
            _mkdir(path, what="export dir")
            path = path / f"{_safe_filename(status.name or info_hash)}.torrent"
        else:
            _mkdir(path.parent, what="export dir")

        data = lt.bencode(lt.create_torrent(ti).generate())
        try:
            # 0644, unlike torsh's own state: an exported .torrent is a file
            # the user means to hand around.
            atomic.write_bytes(path, data, mode=atomic.SHARED)
        except OSError as exc:
            raise EngineError(f"can't write {path}: {exc.strerror or exc}", code="io") from exc
        LOG.info("exported %s → %s", info_hash[:12], path)
        return str(path)

    def set_sequential(self, info_hash: str, enabled: bool) -> None:
        handle = self._handle_for(info_hash)
        if enabled:
            handle.set_flags(lt.torrent_flags.sequential_download)
        else:
            handle.unset_flags(lt.torrent_flags.sequential_download)
        self._refresh_status(info_hash, handle)

    def set_torrent_limits(self, info_hash: str, down: int | None, up: int | None) -> None:
        handle = self._handle_for(info_hash)
        if down is not None:
            handle.set_download_limit(down if down > 0 else -1)
        if up is not None:
            handle.set_upload_limit(up if up > 0 else -1)

    def get_torrent_limits(self, info_hash: str) -> dict[str, int]:
        handle = self._handle_for(info_hash)
        return {
            "down": max(0, handle.download_limit()),
            "up": max(0, handle.upload_limit()),
        }

    def set_global_limits(self, down: int | None, up: int | None) -> None:
        session = self._require_session()
        pack: dict[str, Any] = {}
        with self._lock:
            if down is not None:
                pack["download_rate_limit"] = max(0, down)
                self.config.limits.download = max(0, down)
            if up is not None:
                pack["upload_rate_limit"] = max(0, up)
                self.config.limits.upload = max(0, up)
        if pack:
            session.apply_settings(pack)  # type: ignore[arg-type]

    def set_file_priorities(self, info_hash: str, priorities: Mapping[Any, Any]) -> list[int]:
        handle = self._handle_for(info_hash)
        if not handle.status().has_metadata:
            raise EngineError("metadata not downloaded yet", code="pending")
        with self._lock:  # read-modify-write of the whole vector must not interleave
            current = list(handle.get_file_priorities())
            for raw_index, raw_priority in priorities.items():
                index = int(raw_index)
                if not 0 <= index < len(current):
                    raise EngineError(f"no file #{index}", code="invalid")
                current[index] = _norm_priority(raw_priority)
            handle.prioritize_files(current)
        # prioritize_files commits asynchronously, and libtorrent can drop
        # changes posted while the torrent is still checking. Confirm the
        # commit (re-applying once) so callers can trust the returned vector
        # — `torsh files` right after `torsh prio` must show the new state.
        if not self._await_priorities(handle, current):
            with self._lock:
                handle.prioritize_files(current)
            if not self._await_priorities(handle, current):
                LOG.warning("file priorities not confirmed for %s", info_hash[:12])
        self._refresh_status(info_hash, handle)
        return current

    @staticmethod
    def _await_priorities(handle: lt.torrent_handle, wanted: list[int], timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if list(handle.get_file_priorities()) == wanted:
                    return True
            except Exception:  # noqa: BLE001 - handle may be mid-removal
                return False
            time.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def _refresh_status(self, info_hash: str, handle: lt.torrent_handle) -> None:
        """Pull a fresh status into the cache right after a mutation.

        The alert pump refreshes at ~1 Hz; without this, ``list()`` would
        report pre-mutation state for up to a second after pause/resume/etc.
        """
        try:
            status = handle.status()
        except Exception:  # noqa: BLE001 - handle may be mid-removal
            return
        with self._lock:
            if info_hash in self._status:
                self._status[info_hash] = status

    def torrents(self) -> list[dict[str, Any]]:
        """Every torrent as a view dict (named `torrents`, not `list`, so the
        builtin stays usable in this class's own annotations)."""
        with self._lock:
            statuses = list(self._status.values())
        # No sorting here: every consumer applies its own sort key anyway.
        return [self._view(st) for st in statuses]

    def get(self, info_hash: str) -> dict[str, Any]:
        handle = self._handle_for(info_hash)
        status = handle.status()
        with self._lock:
            # Guarded like _refresh_status: a concurrent remove() must not be
            # undone by re-inserting a ghost row that nothing can clean up.
            if info_hash in self._status:
                self._status[info_hash] = status
        view = self._view(status)
        view.update(
            {
                "pieces_have": status.num_pieces,
                "next_announce": _seconds_or_none(status.next_announce),
                "current_tracker": status.current_tracker,
                "total_size": status.total,
                "active_for": _duration_seconds(status.active_duration),
            }
        )
        ti = handle.torrent_file()
        if ti is not None:
            view.update(
                {
                    "piece_count": ti.num_pieces(),
                    "piece_size": ti.piece_length(),
                    "file_count": ti.files().num_files(),
                    "comment": ti.comment(),
                    "creator": ti.creator(),
                    "private": bool(ti.priv()),
                }
            )
        return view

    def files(self, info_hash: str) -> list[dict[str, Any]]:
        handle = self._handle_for(info_hash)
        ti = handle.torrent_file()
        if ti is None:
            return []
        storage = ti.files()
        progress = handle.file_progress()
        prios = handle.get_file_priorities()
        result = []
        for index in range(storage.num_files()):
            size = storage.file_size(index)
            done = progress[index] if index < len(progress) else 0
            priority = prios[index] if index < len(prios) else 4
            result.append(
                {
                    "index": index,
                    "path": storage.file_path(index),
                    "size": size,
                    "done": done,
                    "progress": (done / size) if size else 1.0,
                    "priority": priority,
                    "priority_label": PRIORITY_LABELS.get(priority, str(priority)),
                }
            )
        return result

    def peers(self, info_hash: str) -> list[dict[str, Any]]:
        handle = self._handle_for(info_hash)
        utp_flag = getattr(lt.peer_info, "utp_socket", 0)
        result = []
        for peer in handle.get_peer_info():
            raw_client = peer.client
            client = (
                raw_client.decode("utf-8", errors="replace") if isinstance(raw_client, bytes) else raw_client
            )
            result.append(
                {
                    "address": f"{peer.ip[0]}:{peer.ip[1]}",
                    "client": client or "—",
                    "down_rate": peer.down_speed,
                    "up_rate": peer.up_speed,
                    "progress": peer.progress,
                    "connection": "uTP" if (utp_flag and peer.flags & utp_flag) else "TCP",
                }
            )
        return result

    def trackers(self, info_hash: str) -> list[dict[str, Any]]:
        handle = self._handle_for(info_hash)
        result = []
        for entry in handle.trackers():
            last_error: Any = entry.get("last_error") or {}
            failed = bool(last_error.get("value"))
            result.append(
                {
                    "url": entry.get("url", ""),
                    "tier": entry.get("tier", 0),
                    "working": not failed and bool(entry.get("verified")),
                    "message": entry.get("message", "") or "",
                    "fails": entry.get("fails", 0),
                    "seeds": max(0, entry.get("scrape_complete", 0) or 0),
                    "leeches": max(0, entry.get("scrape_incomplete", 0) or 0),
                }
            )
        return result

    def session_info(self) -> dict[str, Any]:
        with self._lock:
            statuses = list(self._status.values())
            counters = dict(self._counters)
        down_rate = sum(st.download_payload_rate for st in statuses)
        up_rate = sum(st.upload_payload_rate for st in statuses)
        active = sum(
            1
            for st in statuses
            if not st.paused and st.state not in (_lt_states.checking_files, _lt_states.checking_resume_data)
        )
        return {
            "torrents": len(statuses),
            "active": active,
            "paused": sum(1 for st in statuses if st.paused),
            "down_rate": down_rate,
            "up_rate": up_rate,
            "downloaded": sum(st.all_time_download for st in statuses),
            "uploaded": sum(st.all_time_upload for st in statuses),
            "dht_nodes": counters.get("dht.dht_nodes", 0),
            "listen_port": self.listen_port(),
            "limits": {"down": self.config.limits.download, "up": self.config.limits.upload},
            "uptime": int(time.time() - self._started_at) if self._started_at else 0,
            "engine": f"libtorrent {lt.version}",
        }

    def listen_port(self) -> int:
        session = self._session
        if session is None:
            return 0
        try:
            return session.listen_port()
        except Exception:  # noqa: BLE001
            return 0

    def connect_peer(self, info_hash: str, host: str, port: int) -> None:
        """Manually introduce a peer (useful when DHT/trackers are off)."""
        self._handle_for(info_hash).connect_peer((host, port))

    # ------------------------------------------------------------------
    # View building
    # ------------------------------------------------------------------

    def _view(self, st: lt.torrent_status) -> dict[str, Any]:
        info_hash = _hash_str(st.info_hashes)
        with self._lock:
            torrent_id = self._ids.get(info_hash, 0)
        status = self._derive_status(st)
        rate = st.download_payload_rate
        remaining = max(0, st.total_wanted - st.total_wanted_done)
        if st.is_finished:
            eta = 0
        elif rate > 0:
            eta = int(remaining / rate)
        else:
            eta = -1
        ratio = _ratio_of(st)
        name = st.name or info_hash[:12]
        return {
            "id": torrent_id,
            "hash": info_hash,
            "name": name,
            "status": status,
            "progress": max(0.0, min(1.0, st.progress)),
            "size": st.total_wanted,
            "done": st.total_wanted_done,
            "downloaded": st.all_time_download,
            "uploaded": st.all_time_upload,
            "down_rate": st.download_payload_rate,
            "up_rate": st.upload_payload_rate,
            "eta": eta,
            "ratio": round(ratio, 3),
            "peers": st.num_peers,
            "seeds": st.num_seeds,
            "added": st.added_time,
            "save_path": st.save_path,
            "sequential": bool(st.flags & lt.torrent_flags.sequential_download),
            "has_metadata": st.has_metadata,
            "queue": st.queue_position,
            "error": st.errc.message() if st.errc.value() else "",
        }

    @staticmethod
    def _derive_status(st: lt.torrent_status) -> str:
        if st.errc.value():
            return "error"
        if st.moving_storage:
            return "moving"
        if st.state in (_lt_states.checking_files, _lt_states.checking_resume_data):
            return "checking"
        if st.paused:
            return "queued" if st.auto_managed else "paused"
        if not st.has_metadata or st.state == _lt_states.downloading_metadata:
            return "metadata"
        if st.is_finished:
            return "seeding"
        return "downloading"

    # ------------------------------------------------------------------
    # Alert pump
    # ------------------------------------------------------------------

    def _alert_loop(self) -> None:
        session = self._session
        assert session is not None
        last_post = 0.0
        last_resume = time.time()
        while not self._stop_event.is_set():
            try:
                session.wait_for_alert(400)
                for alert in session.pop_alerts():
                    self._handle_alert(alert)
                now = time.time()
                if now - last_post >= 1.0:
                    session.post_torrent_updates()
                    session.post_session_stats()
                    last_post = now
                if now - last_resume >= self.config.daemon.resume_save_minutes * 60:
                    self._request_resume_saves()
                    last_resume = now
            except Exception:
                LOG.exception("alert loop error")
                time.sleep(0.5)

    def _handle_alert(self, alert: Any) -> None:
        handler = self._dispatch.get(type(alert))
        if handler is None:
            return
        try:
            handler(alert)
        except Exception:
            LOG.exception("alert handler failed for %s", type(alert).__name__)

    def _on_add_torrent(self, alert: lt.add_torrent_alert) -> None:
        if alert.error.value():
            LOG.warning("add torrent failed: %s", alert.error.message())
            with self._lock:
                self._add_failures += 1  # keep _wait_for_registration honest
            return
        self._register(alert.handle)

    def _on_state_update(self, alert: lt.state_update_alert) -> None:
        ratio_limit = self.config.limits.ratio
        with self._lock:
            for st in alert.status:
                self._status[_hash_str(st.info_hashes)] = st
        if ratio_limit > 0:
            self._enforce_ratio(alert.status, ratio_limit)

    def _on_session_stats(self, alert: lt.session_stats_alert) -> None:
        with self._lock:
            self._counters = dict(alert.values)

    def _on_save_resume(self, alert: lt.save_resume_data_alert) -> None:
        info_hash = _hash_str(alert.params.info_hashes)
        if not self._is_registered(info_hash):
            return  # already removed: writing it back would resurrect it
        target = self._resume_file(info_hash)
        # Deliberately outside the lock: with hundreds of torrents the
        # periodic sweep would otherwise hold the status cache hostage for
        # the duration of hundreds of disk writes, stalling every RPC tick.
        atomic.write_bytes(target, lt.write_resume_data_buf(alert.params))
        if not self._is_registered(info_hash):
            # remove() ran while we were writing, so its unlink may have
            # happened before our rename — sweep what we just resurrected.
            target.unlink(missing_ok=True)

    def _is_registered(self, info_hash: str) -> bool:
        with self._lock:
            return info_hash in self._handles

    def _on_save_resume_failed(self, alert: Any) -> None:
        LOG.debug("resume save skipped: %s", getattr(alert, "message", lambda: "?")())

    def _on_metadata(self, alert: lt.metadata_received_alert) -> None:
        # Persist promptly so a magnet's metadata survives an unclean exit.
        alert.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)

    def _on_finished(self, alert: lt.torrent_finished_alert) -> None:
        alert.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
        try:
            name = alert.handle.status().name
        except Exception:  # noqa: BLE001
            name = "?"
        LOG.info("finished: %s", name)

    def _enforce_ratio(self, statuses: Iterable[lt.torrent_status], limit: float) -> None:
        """Pause well-seeded torrents once; a manual resume wins over us."""
        for st in statuses:
            if not st.is_finished or st.paused:
                continue
            ratio = _ratio_of(st)
            if ratio < limit:
                continue
            info_hash = _hash_str(st.info_hashes)
            with self._lock:
                if info_hash in self._ratio_handled:
                    continue
                self._ratio_handled.add(info_hash)
                handle = self._handles.get(info_hash)
            if handle is not None:
                LOG.info("ratio %.2f ≥ %.2f, pausing %s", ratio, limit, st.name)
                handle.unset_flags(lt.torrent_flags.auto_managed)
                handle.pause()

    def _request_resume_saves(self) -> None:
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            try:
                if handle.is_valid() and handle.status().need_save_resume:
                    handle.save_resume_data(
                        lt.save_resume_flags_t.save_info_dict | lt.save_resume_flags_t.only_if_modified
                    )
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------
    # Registration & persistence
    # ------------------------------------------------------------------

    def _register(self, handle: lt.torrent_handle) -> None:
        if not handle.is_valid():
            return
        status = handle.status()
        info_hash = _hash_str(status.info_hashes)
        with self._lock:
            self._handles[info_hash] = handle
            self._status[info_hash] = status
            new_id = info_hash not in self._ids
            if new_id:
                self._ids[info_hash] = self._next_id
                self._next_id += 1
        # Only genuinely new torrents dirty the map — startup resume-loads
        # of known torrents must not rewrite ids.json once per torrent.
        if new_id:
            self._save_ids()

    def _wait_for_registration(self, expected: int, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                settled = len(self._handles) + self._add_failures
                if settled >= expected:
                    return
            time.sleep(0.05)
        LOG.warning("resume load incomplete: %d/%d registered", len(self._handles), expected)

    def _load_resume_files(self) -> int:
        session = self._require_session()
        issued = 0
        # Only a SIGKILL mid-write can leave staging files behind; sweep them
        # here so they can't accumulate over a machine's lifetime.
        dropped = atomic.sweep_stale(paths.resume_dir())
        if dropped:
            LOG.debug("swept %d interrupted resume write(s)", dropped)
        for file in sorted(paths.resume_dir().glob("*.resume")):
            try:
                atp = lt.read_resume_data(file.read_bytes())
            except (RuntimeError, OSError) as exc:
                LOG.warning("corrupt resume file %s: %s", file.name, exc)
                file.rename(file.with_suffix(".corrupt"))
                continue
            session.async_add_torrent(atp)
            issued += 1
        return issued

    def _resume_file(self, info_hash: str) -> Path:
        return paths.resume_dir() / f"{info_hash}.resume"

    def _load_ids(self) -> None:
        """Restore the friendly integer ids shown by ``torsh ls``.

        A damaged map costs the user their numbering, never their torrents —
        so it is repaired here rather than raised.
        """
        self._ids, self._next_id = {}, 1
        file = paths.ids_file()
        try:
            raw = file.read_text(encoding="utf-8")
        except OSError:
            self._ids_loaded = True  # first run: nothing to lose
            return
        try:
            data = json.loads(raw)
            self._ids = {str(k): int(v) for k, v in data["map"].items()}
            self._next_id = max(int(data.get("next", 1)), max(self._ids.values(), default=0) + 1)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            LOG.warning("id map %s is damaged (%s) — renumbering from 1", file.name, exc)
            self._ids, self._next_id = {}, 1
        self._ids_loaded = True

    def _save_ids(self) -> None:
        if not self._ids_loaded:
            # start() failed before it got to read the map. Writing the empty
            # one we hold would replace a user's numbering with nothing.
            return
        # Two locks, outer first: _persist serialises the writers (sync adds
        # and the alert thread both register torrents) so a slower thread's
        # older snapshot can't land last, while _lock is held only long
        # enough to take that snapshot — never across the disk write.
        with self._persist_lock:
            with self._lock:
                payload = json.dumps({"next": self._next_id, "map": self._ids})
            try:
                atomic.write_text(paths.ids_file(), payload)
            except OSError as exc:
                LOG.warning("couldn't persist ids: %s", exc)

    def _load_session_state(self) -> None:
        file = paths.session_state_file()
        try:
            if not file.exists():
                return
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                self._require_session().load_state(lt.bdecode(file.read_bytes()))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - state is a nice-to-have
            LOG.debug("session state not loaded: %s", exc)

    def _save_session_state(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                data = lt.bencode(session.save_state())
            atomic.write_bytes(paths.session_state_file(), data)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("session state not saved: %s", exc)

    # ------------------------------------------------------------------
    # Handle resolution
    # ------------------------------------------------------------------

    def _require_session(self) -> lt.session:
        if self._session is None:
            raise EngineError("engine is not running", code="stopped")
        return self._session

    def _handle_for(self, info_hash: str) -> lt.torrent_handle:
        with self._lock:
            handle = self._handles.get(info_hash)
        if handle is None or not handle.is_valid():
            raise EngineError(f"no torrent with hash {info_hash[:12]}…", code="not_found")
        return handle

    def _resolve_pairs(self, hashes: Iterable[str]) -> list[tuple[str, lt.torrent_handle]]:
        return [(h, self._handle_for(h)) for h in hashes]

    def _resolve_handles(self, hashes: Iterable[str]) -> list[lt.torrent_handle]:
        return [handle for _, handle in self._resolve_pairs(hashes)]


def _duration_seconds(value: Any) -> int:
    """``active_duration`` is a timedelta on modern libtorrent, an int on old."""
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    return int(value)


def _seconds_or_none(value: Any) -> int | None:
    """``next_announce`` comes back as datetime/timedelta/None across versions."""
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return max(0, int(value.total_seconds()))
    if hasattr(value, "timestamp"):
        return max(0, int(value.timestamp() - time.time()))
    return None
