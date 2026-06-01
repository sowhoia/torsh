"""Tests for the async Transmission controller and its mapping helpers.

These lock in the RPC method/attribute names against the installed
``transmission-rpc`` so the regressions fixed here cannot silently return.
"""
from __future__ import annotations

import asyncio
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from torsh.client import TorrentView, TransmissionController
from torsh.config import AppConfig

# Mirrors transmission_rpc.torrent.File (a NamedTuple).
FakeFile = namedtuple("FakeFile", "name size completed priority selected id")


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    speed_limit_down = 500
    speed_limit_down_enabled = True
    speed_limit_up = 50
    speed_limit_up_enabled = False


class FakeTracker:
    host = "tracker.example.com"
    last_announce_result = "Success"
    last_announce_peer_count = 12
    seeder_count = 30
    leecher_count = 4


class FakeTorrent:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.name = kw.get("name", "Ubuntu ISO")
        self.percent_done = kw.get("percent_done", 0.5)
        self.status = kw.get("status", "downloading")
        self.eta = kw.get("eta", timedelta(minutes=3))
        self.rate_download = kw.get("rate_download", 1048576)
        self.rate_upload = kw.get("rate_upload", 0)
        self.ratio = kw.get("ratio", 1.25)
        self.total_size = kw.get("total_size", 2 * 1024**3)
        self.added_date = kw.get("added_date", datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.download_dir = kw.get("download_dir", "/downloads")
        self.peers_connected = kw.get("peers_connected", 8)
        self.peers_sending_to_us = kw.get("peers_sending_to_us", 5)
        self.peers_getting_from_us = kw.get("peers_getting_from_us", 2)
        self.error = kw.get("error", 0)
        self.error_string = kw.get("error_string", "")
        self.download_limit = kw.get("download_limit", 200)
        self.download_limited = kw.get("download_limited", True)
        self.upload_limit = kw.get("upload_limit", 100)
        self.upload_limited = kw.get("upload_limited", False)
        self._files = kw.get("files", [])
        self.tracker_stats = kw.get("tracker_stats", [FakeTracker()])

    def get_files(self):
        return self._files


class FakeClient:
    """Records mutations so tests can assert the exact RPC contract."""

    def __init__(self, torrents=None, torrent=None):
        self._torrents = torrents or []
        self._torrent = torrent or FakeTorrent()
        self.calls: dict[str, dict] = {}

    def get_session(self):
        return FakeSession()

    def set_session(self, **kwargs):
        self.calls["set_session"] = kwargs

    def session_stats(self):
        return {"download_speed": 1024}

    def get_torrents(self):
        return self._torrents

    def get_torrent(self, tid, **_):
        return self._torrent

    def change_torrent(self, ids, **kwargs):
        self.calls["change_torrent"] = {"ids": ids, **kwargs}

    def add_torrent(self, link, **kwargs):
        self.calls["add_torrent"] = {"link": link, **kwargs}
        return FakeTorrent(id=99, name="added")

    def start_torrent(self, ids, **kwargs):
        self.calls["start_torrent"] = {"ids": ids, **kwargs}


def make_controller(client: FakeClient) -> TransmissionController:
    ctrl = TransmissionController(AppConfig(), retries=0)
    ctrl._client = client
    return ctrl


# --------------------------------------------------------------------------
# Pure mapping helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "—"),
        (timedelta(seconds=0), "—"),
        (timedelta(minutes=2), "2 minutes"),
        (-1, "—"),       # raw "unavailable"
        (-2, "∞"),       # raw "unknown"
        (90, "2 minutes"),
    ],
)
def test_format_eta(value, expected):
    assert TransmissionController._format_eta(value) == expected


def test_format_eta_never_raises_on_timedelta():
    # Regression: ``timedelta > 0`` used to raise TypeError mid-refresh.
    assert TransmissionController._format_eta(timedelta(hours=5))


def test_percent_done_from_fraction():
    assert TransmissionController._percent_done(FakeTorrent(percent_done=0.42)) == pytest.approx(42.0)


def test_percent_done_clamped():
    assert TransmissionController._percent_done(FakeTorrent(percent_done=2.0)) == 100.0


def test_natural_rate_and_coercion():
    assert TransmissionController._natural_rate(1048576) == "1.0 MiB/s"
    assert TransmissionController._natural_rate(None).startswith("0 ")
    assert TransmissionController._as_int("bad") == 0
    assert TransmissionController._as_float(None) is None


# --------------------------------------------------------------------------
# Mapping a full torrent
# --------------------------------------------------------------------------

def test_map_torrent_produces_view():
    ctrl = make_controller(FakeClient())
    view = ctrl._map_torrent(FakeTorrent(error=1, error_string="tracker gone"))
    assert isinstance(view, TorrentView)
    assert view.percent_done == pytest.approx(50.0)
    assert view.eta == "3 minutes"
    assert view.rate_down == "1.0 MiB/s"
    assert view.error is True
    assert view.error_string == "tracker gone"


def test_list_torrents_roundtrip():
    client = FakeClient(torrents=[FakeTorrent(id=1), FakeTorrent(id=2)])
    views = run(make_controller(client).list_torrents())
    assert [v.id for v in views] == [1, 2]


# --------------------------------------------------------------------------
# Speed limits — guards the attribute/kwarg names
# --------------------------------------------------------------------------

def test_get_speed_limits_uses_correct_attrs():
    limits = run(make_controller(FakeClient()).get_speed_limits())
    assert limits == {"down": 500, "up": 0}  # up disabled -> 0


def test_set_speed_limits_emits_spec_kwargs():
    client = FakeClient()
    run(make_controller(client).set_speed_limits(300, 0))
    assert client.calls["set_session"] == {
        "speed_limit_down_enabled": True,
        "speed_limit_down": 300,
        "speed_limit_up_enabled": False,
        "speed_limit_up": 0,
    }


def test_torrent_speed_uses_change_torrent():
    client = FakeClient()
    run(make_controller(client).set_torrent_speed(7, 256, 0))
    call = client.calls["change_torrent"]
    assert call["ids"] == 7
    assert call["download_limit"] == 256 and call["download_limited"] is True
    assert call["upload_limited"] is False


def test_get_torrent_speed_respects_enabled_flags():
    speed = run(make_controller(FakeClient()).get_torrent_speed(1))
    assert speed == {"down": 200, "up": 0}  # upload_limited is False -> 0


# --------------------------------------------------------------------------
# Files & priorities
# --------------------------------------------------------------------------

def test_get_files_normalizes_namedtuples():
    files = [
        FakeFile(name="a.mkv", size=1000, completed=500, priority=1, selected=True, id=0),
        FakeFile(name="b.nfo", size=20, completed=20, priority=-1, selected=False, id=1),
    ]
    client = FakeClient(torrent=FakeTorrent(files=files))
    result = run(make_controller(client).get_files(3))
    assert result[0] == {"name": "a.mkv", "length": 1000, "bytesCompleted": 500, "priority": 1}
    assert result[1]["priority"] == -1


def test_set_priority_uses_change_torrent():
    client = FakeClient()
    run(make_controller(client).set_priority(5, [0, 1], [2], [3]))
    call = client.calls["change_torrent"]
    assert call["ids"] == 5
    assert call["priority_high"] == [0, 1]
    assert call["priority_low"] == [3]


# --------------------------------------------------------------------------
# Trackers
# --------------------------------------------------------------------------

def test_get_trackers_from_objects():
    client = FakeClient(torrent=FakeTorrent(tracker_stats=[FakeTracker()]))
    trackers = run(make_controller(client).get_trackers(1))
    assert trackers[0]["host"] == "tracker.example.com"
    assert trackers[0]["seeders"] == 30
    assert trackers[0]["peers"] == 12


# --------------------------------------------------------------------------
# Mutations call through with the right argument shapes
# --------------------------------------------------------------------------

def test_add_starts_unpaused():
    client = FakeClient()
    run(make_controller(client).add("magnet:?xt=urn:btih:abc", "/tmp"))
    call = client.calls["add_torrent"]
    assert call["paused"] is False
    assert call["download_dir"] == "/tmp"


def test_start_passes_bypass_queue():
    client = FakeClient()
    run(make_controller(client).start([1, 2]))
    assert client.calls["start_torrent"] == {"ids": [1, 2], "bypass_queue": True}
