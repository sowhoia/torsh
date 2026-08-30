"""CLI rendering: pure functions over torrent dicts, checked via rich capture."""

from __future__ import annotations

from typing import Any

from rich.console import Console

from torsh.cli import render


def _capture(renderable: Any, width: int = 120) -> str:
    console = Console(width=width, force_terminal=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _torrent(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "name": "payload",
        "progress": 0.5,
        "status": "downloading",
        "size": 1024**2,
        "done": 512 * 1024,
        "down_rate": 10_240,
        "up_rate": 0,
        "eta": 51,
        "ratio": 0.25,
        "peers": 3,
        "seeds": 1,
    }
    base.update(overrides)
    return base


def test_progress_cell_shows_metadata_state():
    assert "fetching" in _capture(render.progress_cell(0.0, "metadata"))


def test_progress_cell_shows_bar_otherwise():
    out = _capture(render.progress_cell(0.5, "downloading"))
    assert "50.0%" in out


def test_torrent_table_adapts_and_formats():
    wide = _capture(render.torrent_table([_torrent()], width=120))
    assert "payload" in wide
    assert "10.0 KiB/s" in wide
    assert "downloading" in wide
    assert "1/3" in wide  # seeds/peers column present in the wide layout

    narrow = _capture(render.torrent_table([_torrent()], width=60), width=60)
    assert "payload" in narrow
    assert "10.0 KiB/s" not in narrow  # rate column dropped when space is tight


def _detail(**overrides: Any) -> dict[str, Any]:
    base = _torrent(hash="e2" * 20, downloaded=512 * 1024, uploaded=0, save_path="/srv/downloads")
    base.update(overrides)
    return base


def test_detail_panel_shows_queue_position_only_when_queued():
    assert "queue" in _capture(render.detail_panel(_detail(queue=3, status="queued")))
    # -1 is libtorrent's "not queued"; a bogus "#-1" row would just confuse.
    assert "queue" not in _capture(render.detail_panel(_detail(queue=-1)))


def test_watch_view_shows_session_header_and_hint():
    session = {
        "torrents": 2,
        "down_rate": 2048,
        "up_rate": 0,
        "limits": {"down": 2 * 1024**2, "up": 0},
        "dht_nodes": 42,
        "listen_port": 6881,
    }
    out = _capture(render.watch_view([_torrent()], session))
    assert "2" in out and "torrents" in out
    assert "dht 42" in out
    assert "limit" in out  # a set limit is surfaced
    assert "1/2 shown" in out
    assert "Ctrl+C" in out


def test_watch_view_empty_state():
    session = {"torrents": 0, "down_rate": 0, "up_rate": 0, "limits": {}, "dht_nodes": 0, "listen_port": 0}
    out = _capture(render.watch_view([], session))
    assert "nothing to show" in out


def test_detail_panel_surfaces_an_error_and_streaming_mode():
    out = _capture(render.detail_panel(_detail(status="error", error="disk is full", sequential=True)))
    assert "disk is full" in out
    assert "sequential" in out


def test_session_panel_shows_unlimited_as_infinity():
    out = _capture(
        render.session_panel(
            {
                "torrents": 3,
                "active": 2,
                "paused": 1,
                "down_rate": 1024,
                "up_rate": 0,
                "downloaded": 2048,
                "uploaded": 512,
                "limits": {"down": 0, "up": 2 * 1024**2},
                "dht_nodes": 7,
                "listen_port": 6881,
                "engine": "libtorrent 2.0.11",
            }
        )
    )
    assert "∞" in out  # no download limit set
    assert "2.0 MiB/s" in out
    assert "libtorrent 2.0.11" in out


def test_files_table_labels_priorities():
    files = [
        {"index": 0, "path": "disc/movie.mkv", "size": 1024**3, "progress": 0.5, "priority_label": "high"},
        {"index": 1, "path": "disc/sample.mkv", "size": 1024, "progress": 0.0, "priority_label": "skip"},
    ]
    out = _capture(render.files_table(files))
    assert "movie.mkv" in out and "high" in out
    assert "sample.mkv" in out and "skip" in out
    assert "1.0 GiB" in out


def test_peers_table_shows_transport_and_rates():
    peers = [
        {
            "address": "10.0.0.5:51413",
            "client": "qBittorrent 4.6",
            "down_rate": 2048,
            "up_rate": 0,
            "progress": 1.0,
            "connection": "uTP",
        }
    ]
    out = _capture(render.peers_table(peers))
    assert "10.0.0.5:51413" in out
    assert "2.0 KiB/s" in out
    assert "100.0%" in out
    assert "uTP" in out


def test_trackers_table_distinguishes_working_failing_and_pending():
    trackers = [
        {
            "url": "http://ok.example/announce",
            "tier": 0,
            "working": True,
            "message": "",
            "fails": 0,
            "seeds": 12,
            "leeches": 3,
        },
        {
            "url": "http://bad.example/announce",
            "tier": 1,
            "working": False,
            "message": "timed out",
            "fails": 4,
            "seeds": 0,
            "leeches": 0,
        },
        {
            "url": "http://new.example/announce",
            "tier": 2,
            "working": False,
            "message": "",
            "fails": 0,
            "seeds": 0,
            "leeches": 0,
        },
    ]
    out = _capture(render.trackers_table(trackers))
    assert "✓ ok" in out
    assert "timed out" in out
    assert "waiting" in out
    assert "12" in out
    assert "—" in out  # no scrape numbers yet for the others


def test_both_renderers_know_every_status_the_engine_can_emit():
    """A new engine status with no glyph renders as a meaningless bullet.

    The vocabulary is read out of the engine itself so adding a status
    without teaching the CLI and the TUI about it fails here.
    """
    import inspect
    import re

    from torsh.engine import Engine
    from torsh.tui.app import STATUS_GLYPHS

    source = inspect.getsource(Engine._derive_status)
    emitted = set(re.findall(r'"([a-z]+)"', source))
    assert len(emitted) >= 8, f"the status vocabulary moved: {emitted}"
    assert emitted <= set(render.STATUS_STYLE), "the CLI is missing a status"
    assert emitted <= set(STATUS_GLYPHS), "the TUI is missing a status"
    assert set(render.STATUS_STYLE) == set(STATUS_GLYPHS), "the two surfaces disagree"
