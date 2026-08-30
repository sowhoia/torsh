"""Engine integration tests against real libtorrent sessions.

The centrepiece is a genuine localhost transfer: a bare libtorrent seeder on
one side, the torsh :class:`Engine` downloading on the other.  If that
passes, add/status/progress/resume/files all work for real — no mocks.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import libtorrent as lt
import pytest

from torsh.config import Config
from torsh.engine import PRIORITY_NAMES, Engine, EngineError

PIECE = 16 * 1024


def make_torrent(directory: Path, name: str = "payload.bin", size: int = PIECE * 40) -> tuple[Path, bytes]:
    """Create a random payload plus its bencoded .torrent metadata."""
    payload = directory / name
    payload.write_bytes(os.urandom(size))
    fs = lt.file_storage()
    lt.add_files(fs, str(payload))
    creator = lt.create_torrent(fs, PIECE)
    lt.set_piece_hashes(creator, str(directory))
    return payload, lt.bencode(creator.generate())


def quiet_config(tmp_path: Path) -> Config:
    config = Config()
    config.downloads.dir = str(tmp_path / "downloads")
    config.network.dht = False
    config.network.upnp = False
    config.network.natpmp = False
    config.network.lsd = False
    return config


@pytest.fixture
def engine(tmp_path):
    eng = Engine(quiet_config(tmp_path))
    eng.start()
    yield eng
    eng.stop()


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


# ---------------------------------------------------------------------------
# Adding & basic lifecycle
# ---------------------------------------------------------------------------


def test_add_torrent_file_and_list(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    torrent_file = tmp_path / "payload.torrent"
    torrent_file.write_bytes(meta)

    view = engine.add(str(torrent_file), paused=True)
    assert view["id"] == 1
    assert view["name"] == "payload.bin"
    assert view["status"] in {"paused", "checking"}
    assert view["size"] == PIECE * 40
    assert len(view["hash"]) == 40

    listed = engine.torrents()
    assert len(listed) == 1
    assert listed[0]["hash"] == view["hash"]


def test_add_magnet_shows_metadata_state(engine):
    view = engine.add(
        "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&dn=ghost",
        paused=False,
    )
    assert view["name"] == "ghost"
    assert view["has_metadata"] is False


def test_add_raw_infohash(engine):
    view = engine.add("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert view["hash"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_duplicate_add_is_a_clean_error(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    torrent_file = tmp_path / "dup.torrent"
    torrent_file.write_bytes(meta)

    engine.add(str(torrent_file), paused=True)
    with pytest.raises(EngineError) as excinfo:
        engine.add(str(torrent_file), paused=True)
    assert excinfo.value.code == "duplicate"


def test_add_garbage_is_a_clean_error(engine):
    with pytest.raises(EngineError) as excinfo:
        engine.add("definitely not a torrent")
    assert excinfo.value.code == "invalid"


def test_fetch_url_rejects_non_http_schemes():
    with pytest.raises(EngineError) as excinfo:
        Engine._fetch_url("file:///etc/passwd")
    assert excinfo.value.code == "invalid"


def test_pause_resume_and_remove(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "t.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "t.torrent"))
    info_hash = view["hash"]

    assert engine.pause([info_hash]) == 1
    wait_for(lambda: engine.get(info_hash)["status"] == "paused")

    assert engine.resume([info_hash]) == 1
    wait_for(lambda: engine.get(info_hash)["status"] != "paused")

    assert engine.remove([info_hash]) == 1
    assert engine.torrents() == []
    with pytest.raises(EngineError):
        engine.get(info_hash)


def test_unknown_hash_raises_not_found(engine):
    with pytest.raises(EngineError) as excinfo:
        engine.pause(["f" * 40])
    assert excinfo.value.code == "not_found"


def test_pause_reflects_in_list_immediately(engine, tmp_path):
    """Mutations must refresh the status cache — no ~1s stale window."""
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "fresh.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "fresh.torrent"))

    engine.pause([view["hash"]])
    listed = {t["hash"]: t for t in engine.torrents()}[view["hash"]]
    assert listed["status"] == "paused"

    engine.resume([view["hash"]])
    listed = {t["hash"]: t for t in engine.torrents()}[view["hash"]]
    assert listed["status"] != "paused"


def test_add_rejects_relative_download_dir(engine, tmp_path):
    """A relative -d would resolve against the daemon's cwd — refuse it."""
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "rel.torrent").write_bytes(meta)
    with pytest.raises(EngineError) as excinfo:
        engine.add(str(tmp_path / "rel.torrent"), download_dir="relative/dl", paused=True)
    assert excinfo.value.code == "invalid"


def test_move_rejects_relative_path(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "mv.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "mv.torrent"), paused=True)
    with pytest.raises(EngineError) as excinfo:
        engine.move(view["hash"], "relative/subdir")
    assert excinfo.value.code == "invalid"


def test_add_reports_an_unusable_download_dir_as_io(engine, tmp_path):
    """A dir that can't be created is the user's problem, not an ICE."""
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "io.torrent").write_bytes(meta)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with pytest.raises(EngineError) as excinfo:
        engine.add(str(tmp_path / "io.torrent"), download_dir=str(blocker / "sub"), paused=True)
    assert excinfo.value.code == "io"


def test_duplicate_error_carries_hash(engine, tmp_path):
    """Frontends attach to duplicates by structured hash, not message text."""
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "dup2.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "dup2.torrent"), paused=True)
    with pytest.raises(EngineError) as excinfo:
        engine.add(str(tmp_path / "dup2.torrent"), paused=True)
    assert excinfo.value.data.get("hash") == view["hash"]


def test_sequential_toggle_and_limits(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "seq.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "seq.torrent"), paused=True, sequential=False)
    info_hash = view["hash"]
    assert view["sequential"] is False

    engine.set_sequential(info_hash, True)
    assert engine.get(info_hash)["sequential"] is True

    engine.set_torrent_limits(info_hash, 100 * 1024, 0)
    assert engine.get_torrent_limits(info_hash) == {"down": 100 * 1024, "up": 0}

    engine.set_global_limits(2 * 1024**2, 512 * 1024)
    info = engine.session_info()
    assert info["limits"] == {"down": 2 * 1024**2, "up": 512 * 1024}


# ---------------------------------------------------------------------------
# Queue & export
# ---------------------------------------------------------------------------


def test_queue_moves_reorder_torrents(engine, tmp_path):
    hashes = []
    for index in range(3):
        src = tmp_path / f"q{index}"
        src.mkdir()
        _, meta = make_torrent(src, name=f"q{index}.bin", size=PIECE * 4)
        (tmp_path / f"q{index}.torrent").write_bytes(meta)
        hashes.append(engine.add(str(tmp_path / f"q{index}.torrent"))["hash"])

    last = hashes[-1]
    assert engine.get(last)["queue"] == 2
    assert engine.set_queue_position(last, "top") == 0
    assert engine.get(last)["queue"] == 0
    assert engine.set_queue_position(last, "down") == 1
    assert engine.set_queue_position(last, "bottom") == 2

    with pytest.raises(EngineError) as excinfo:
        engine.set_queue_position(last, "sideways")
    assert excinfo.value.code == "invalid"


def test_export_writes_a_loadable_torrent(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "src.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "src.torrent"), paused=True)

    out_dir = tmp_path / "exports"
    written = Path(engine.export(view["hash"], str(out_dir)))
    assert written.parent == out_dir
    assert written.name == "payload.bin.torrent"
    # The whole point: the export must be a real torrent for the same data.
    assert str(lt.torrent_info(str(written)).info_hashes().get_best()) == view["hash"]

    explicit = tmp_path / "exports" / "chosen.torrent"
    assert engine.export(view["hash"], str(explicit)) == str(explicit)
    assert explicit.exists()
    assert not list(out_dir.glob("*.tmp"))


def test_export_rejects_relative_paths_and_bare_magnets(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "rel-export.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "rel-export.torrent"), paused=True)
    with pytest.raises(EngineError) as excinfo:
        engine.export(view["hash"], "somewhere/relative")
    assert excinfo.value.code == "invalid"

    magnet = engine.add("magnet:?xt=urn:btih:" + "c" * 40 + "&dn=ghost")
    with pytest.raises(EngineError) as excinfo:
        engine.export(magnet["hash"], str(tmp_path))
    assert excinfo.value.code == "pending"


def test_export_sanitises_hostile_names(tmp_path):
    from torsh.engine import _safe_filename

    # A torrent name comes from a stranger: it must never steer the write.
    # One path component with no separators can't climb out of the target dir.
    hostile = _safe_filename("../../etc/passwd")
    assert Path(hostile).name == hostile
    assert not {"/", "\\"} & set(hostile)
    assert _safe_filename("a/b\\c:d") == "a_b_c_d"
    assert _safe_filename("  . ") == "torrent"
    assert _safe_filename("nul\x00byte") == "nul_byte"
    assert len(_safe_filename("x" * 400)) == 120


# ---------------------------------------------------------------------------
# Files & priorities
# ---------------------------------------------------------------------------


def test_files_and_priorities(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src, size=PIECE * 8)
    (tmp_path / "files.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "files.torrent"), paused=True)

    files = engine.files(view["hash"])
    assert len(files) == 1
    assert files[0]["size"] == PIECE * 8
    assert files[0]["priority_label"] == "normal"

    # The engine confirms the async commit — the effect is visible on return.
    engine.set_file_priorities(view["hash"], {0: "high"})
    assert engine.files(view["hash"])[0]["priority"] == PRIORITY_NAMES["high"]

    engine.set_file_priorities(view["hash"], {0: 0})
    assert engine.files(view["hash"])[0]["priority_label"] == "skip"

    with pytest.raises(EngineError):
        engine.set_file_priorities(view["hash"], {5: "high"})
    with pytest.raises(EngineError):
        engine.set_file_priorities(view["hash"], {0: "sideways"})


def test_remove_survives_in_flight_resume_save(engine, tmp_path):
    """A resume save racing remove() must not resurrect the .resume file."""
    from torsh import paths

    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "race.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "race.torrent"), paused=True)
    info_hash = view["hash"]
    resume_file = paths.resume_dir() / f"{info_hash}.resume"
    wait_for(lambda: resume_file.exists())  # add() persists metadata promptly

    # Queue one more save, then remove before its alert is processed.
    import libtorrent as _lt

    engine._handles[info_hash].save_resume_data(_lt.save_resume_flags_t.save_info_dict)
    engine.remove([info_hash])
    assert not resume_file.exists()

    time.sleep(1.5)  # let the in-flight alert land — it must be ignored
    assert not resume_file.exists()


# ---------------------------------------------------------------------------
# Input handling: what counts as a torrent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("e" * 40, True),  # hex v1/v2 infohash
        ("E" * 40, True),  # ...case-insensitively
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", True),  # base32 v1
        ("abcdefghijklmnopqrstuvwxyz234567", True),
        ("z" * 40, False),  # 40 chars, not hex
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ2345678", False),  # 33 chars
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ23456!", False),  # right length, wrong alphabet
        ("ПРИВЕТМИРПРИВЕТМИРПРИВЕТМИРПРИВЕ", False),  # 32 letters, but not base32
        ("ubuntu", False),
    ],
)
def test_looks_like_hash_is_strict(text, expected):
    """`str.isalnum()` would wave through any 32-character word, turning a
    typo into a baffling "not a valid torrent" from the magnet parser."""
    from torsh.engine import _looks_like_hash

    assert _looks_like_hash(text) is expected


def _serve_bytes(payload: bytes):
    """A one-shot localhost HTTP server: `/torrent` serves *payload*,
    `/redirect` bounces to another scheme."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # http.server's own naming convention
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "ftp://example.invalid/evil.torrent")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # keep pytest output clean

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_add_from_an_http_url(engine, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    server = _serve_bytes(meta)
    try:
        port = server.server_address[1]
        view = engine.add(f"http://127.0.0.1:{port}/torrent", paused=True)
        assert view["name"] == "payload.bin"
    finally:
        server.shutdown()


def test_a_torrent_url_may_not_redirect_off_http(engine):
    """A checked scheme is worth nothing if a 302 can change it: urllib
    follows redirects into ftp:// by default."""
    server = _serve_bytes(b"")
    try:
        port = server.server_address[1]
        with pytest.raises(EngineError) as excinfo:
            engine.add(f"http://127.0.0.1:{port}/redirect")
        assert excinfo.value.code == "io"
        assert "refusing redirect" in str(excinfo.value)
    finally:
        server.shutdown()


def test_an_oversized_download_is_refused(engine, tmp_path, monkeypatch):
    """A .torrent is kilobytes; anything huge is a mistake or a tarpit."""
    monkeypatch.setattr("torsh.engine._TORRENT_URL_MAX_BYTES", 32)
    server = _serve_bytes(b"x" * 4096)
    try:
        port = server.server_address[1]
        with pytest.raises(EngineError) as excinfo:
            engine.add(f"http://127.0.0.1:{port}/torrent")
        assert excinfo.value.code == "invalid"
        assert "larger than" in str(excinfo.value)
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_start_twice_is_refused(engine):
    with pytest.raises(EngineError) as excinfo:
        engine.start()
    assert excinfo.value.code == "running"


def test_a_failed_start_leaves_nothing_running(tmp_path, monkeypatch):
    """Half a started engine is worse than none: the caller sees the error
    but the alert pump would keep running a session nobody owns."""

    def boom(self, expected, timeout=8.0):
        raise RuntimeError("startup went wrong after the pump began")

    monkeypatch.setattr(Engine, "_wait_for_registration", boom)
    engine = Engine(quiet_config(tmp_path))
    with pytest.raises(RuntimeError, match="startup went wrong"):
        engine.start()

    assert engine._session is None
    assert engine._alert_thread is None
    assert not [t for t in threading.enumerate() if t.name == "torsh-alerts"]


def test_a_failed_start_never_overwrites_the_id_map(tmp_path, monkeypatch):
    """Startup failure must not cost the user their `torsh ls` numbering.

    stop() runs on the failure path and persists state; if it fired before
    ids.json had been read, it would write the empty map it happens to hold.
    """
    from torsh import paths

    paths.ensure_dirs()
    paths.ids_file().parent.mkdir(parents=True, exist_ok=True)
    original = '{"next": 9, "map": {"' + "a" * 40 + '": 8}}'
    paths.ids_file().write_text(original, encoding="utf-8")

    def boom(self):
        raise OSError("the state dir went away mid-start")

    monkeypatch.setattr(Engine, "_load_session_state", boom)
    engine = Engine(quiet_config(tmp_path))
    with pytest.raises(OSError):
        engine.start()

    assert paths.ids_file().read_text(encoding="utf-8") == original


def test_a_damaged_id_map_costs_numbering_not_torrents(engine, tmp_path):
    """ids.json is a convenience; garbage in it must not block startup."""
    from torsh import paths

    engine.stop()  # release the fixture's session before reusing the home
    paths.ids_file().write_text('["not", "a", "map"]', encoding="utf-8")

    reborn = Engine(quiet_config(tmp_path))
    reborn.start()
    try:
        src = tmp_path / "afterwards"
        src.mkdir()
        _, meta = make_torrent(src)
        (tmp_path / "after.torrent").write_bytes(meta)
        assert reborn.add(str(tmp_path / "after.torrent"), paused=True)["id"] == 1
    finally:
        reborn.stop()


def test_a_stopped_engine_owns_nothing_and_can_start_again(tmp_path):
    engine = Engine(quiet_config(tmp_path))
    engine.start()
    try:
        src = tmp_path / "src"
        src.mkdir()
        _, meta = make_torrent(src)
        (tmp_path / "cycle.torrent").write_bytes(meta)
        info_hash = engine.add(str(tmp_path / "cycle.torrent"), paused=True)["hash"]
    finally:
        engine.stop()

    assert engine.torrents() == []
    with pytest.raises(EngineError) as excinfo:
        engine.add("magnet:?xt=urn:btih:" + "d" * 40)
    assert excinfo.value.code == "stopped"

    engine.start()  # ...and the same object comes back up cleanly
    try:
        assert wait_for(lambda: engine.torrents())[0]["hash"] == info_hash
    finally:
        engine.stop()


def test_apply_config_reaches_the_live_session(engine, tmp_path):
    port_before = engine.listen_port()
    fresh = quiet_config(tmp_path)
    fresh.limits.download = 3 * 1024**2
    fresh.network.max_connections = 123
    fresh.network.port = port_before + 1 if port_before else 6881
    engine.apply_config(fresh)

    settings = engine._session.get_settings()
    assert settings["download_rate_limit"] == 3 * 1024**2
    assert settings["connections_limit"] == 123
    # The port is the one setting held back: re-binding sockets mid-flight
    # would drop every connected peer for a setting nobody asked to take
    # effect right now.
    assert engine.listen_port() == port_before


# ---------------------------------------------------------------------------
# Restart persistence (fast-resume)
# ---------------------------------------------------------------------------


def test_resume_data_is_written_privately(engine, tmp_path):
    """Resume blobs say what you are downloading — 0600, like the state dir."""
    from torsh import paths

    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    (tmp_path / "priv.torrent").write_bytes(meta)
    view = engine.add(str(tmp_path / "priv.torrent"), paused=True)

    resume_file = paths.resume_dir() / f"{view['hash']}.resume"
    wait_for(resume_file.exists)
    assert stat.S_IMODE(resume_file.stat().st_mode) == 0o600
    assert not list(paths.resume_dir().glob(".*.tmp")), "no staging litter"


def test_torrents_survive_engine_restart(tmp_path):
    engine = Engine(quiet_config(tmp_path))
    engine.start()
    try:
        src = tmp_path / "src"
        src.mkdir()
        _, meta = make_torrent(src)
        (tmp_path / "keep.torrent").write_bytes(meta)
        first = engine.add(str(tmp_path / "keep.torrent"), paused=True)
    finally:
        engine.stop()

    reborn = Engine(quiet_config(tmp_path))
    reborn.start()
    try:
        views = wait_for(lambda: reborn.torrents())
        assert len(views) == 1
        assert views[0]["hash"] == first["hash"]
        assert views[0]["name"] == "payload.bin"
        assert views[0]["id"] == first["id"]  # ids are stable across restarts
    finally:
        reborn.stop()


# ---------------------------------------------------------------------------
# The real thing: a localhost transfer
# ---------------------------------------------------------------------------


def test_localhost_download_end_to_end(engine, tmp_path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    payload, meta = make_torrent(seed_dir, size=PIECE * 64)
    torrent_file = tmp_path / "xfer.torrent"
    torrent_file.write_bytes(meta)

    # Plain libtorrent seeder on localhost — deliberately not our Engine.
    seeder = lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
            "alert_mask": lt.alert.category_t.status_notification,
        }
    )
    atp = lt.load_torrent_buffer(meta)
    atp.save_path = str(seed_dir)
    seed_handle = seeder.add_torrent(atp)

    def seeder_ready():
        status = seed_handle.status()
        return status.is_seeding or status.state == lt.torrent_status.states.seeding

    wait_for(seeder_ready, timeout=20)
    seed_port = seeder.listen_port()

    view = engine.add(str(torrent_file))
    info_hash = view["hash"]

    # No tracker/DHT in play: introduce the peer by hand (public API).
    engine.connect_peer(info_hash, "127.0.0.1", seed_port)

    done = wait_for(lambda: engine.get(info_hash)["progress"] >= 1.0, timeout=60)
    assert done

    final = engine.get(info_hash)
    assert final["status"] == "seeding"
    assert final["eta"] == 0

    downloaded = Path(final["save_path"]) / "payload.bin"
    assert downloaded.read_bytes() == payload.read_bytes()

    info = engine.session_info()
    assert info["torrents"] == 1
    assert info["engine"].startswith("libtorrent")

    # A finished torrent has left the queue; reordering it must say so rather
    # than no-op and report libtorrent's -1 sentinel as a position.
    assert final["queue"] == -1
    with pytest.raises(EngineError) as excinfo:
        engine.set_queue_position(info_hash, "top")
    assert excinfo.value.code == "invalid"
    assert "queue" in str(excinfo.value)
