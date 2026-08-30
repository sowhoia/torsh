"""TUI smoke tests with Textual's headless pilot against a live daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_daemon import running_daemon  # noqa: F401 - fixture reuse
from tests.test_engine import make_torrent
from torsh import config as config_mod
from torsh import ipc
from torsh.tui.app import TorshApp, _expand_uri


def test_expand_uri_normalises_local_paths(tmp_path):
    """The daemon has its own cwd — the TUI must send absolute paths."""
    torrent = tmp_path / "a.torrent"
    torrent.write_bytes(b"x")
    assert _expand_uri(str(torrent)) == str(torrent.resolve())
    assert _expand_uri("magnet:?xt=urn:btih:abc") == "magnet:?xt=urn:btih:abc"
    assert _expand_uri("https://example.org/a.torrent") == "https://example.org/a.torrent"
    assert _expand_uri("no-such-file.torrent") == "no-such-file.torrent"


def test_modals_do_not_shadow_textual_internals():
    """`_name` belongs to Textual's DOMNode — modals must not squat on it."""
    from torsh.tui.modals import MoveModal, PrioritiesModal

    move = MoveModal("ubuntu.iso", "/srv/downloads")
    assert move.name is None
    assert move._torrent_name == "ubuntu.iso"

    priorities = PrioritiesModal("ubuntu.iso", [])
    assert priorities.name is None
    assert priorities._torrent_name == "ubuntu.iso"


@pytest.fixture
def app(running_daemon):  # noqa: F811
    return TorshApp(config_mod.load())


@pytest.fixture
def torrent_added(running_daemon, tmp_path):  # noqa: F811
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    file = tmp_path / "tui.torrent"
    file.write_bytes(meta)
    with ipc.Client() as client:
        view = client.call("add", uri=str(file), paused=True)
    return view


@pytest.mark.asyncio
async def test_boot_renders_and_connects(app):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        assert app.connected is True
        assert app.session["engine"].startswith("libtorrent")


@pytest.mark.asyncio
async def test_table_shows_torrent(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        table = app.query_one("#torrents")
        assert table.row_count == 1
        assert app.view_rows[0]["name"] == "payload.bin"


@pytest.mark.asyncio
async def test_filter_narrows_list(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.press("slash")
        await pilot.pause()
        assert app.query_one("#filter-input").has_focus
        await pilot.press("z", "z", "z")
        await pilot.pause()
        assert app.view_rows == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.filter_text == ""
        assert len(app.view_rows) == 1


@pytest.mark.asyncio
async def test_selection_and_pause_resume(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        await pilot.press("x")
        assert len(app.selected) == 1
        await pilot.press("escape")
        assert app.selected == set()

        # resume the paused torrent via space (smart toggle); the daemon's
        # status cache refreshes at ~1 Hz, so poll for the transition.
        await pilot.press("space")
        statuses: set[str] = set()
        for _ in range(30):
            await pilot.pause(0.3)
            await app.refresh_data()
            statuses = {t["status"] for t in app.torrents}
            if statuses & {"downloading", "queued", "checking", "metadata", "seeding"}:
                break
        assert statuses & {"downloading", "queued", "checking", "metadata", "seeding"}


@pytest.mark.asyncio
async def test_add_modal_opens_and_cancels(app):
    from torsh.tui.modals import AddTorrentModal

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("a")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AddTorrentModal)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, AddTorrentModal)


@pytest.mark.asyncio
async def test_help_modal(app):
    from torsh.tui.modals import HelpModal

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause(0.3)
        assert isinstance(app.screen, HelpModal)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, HelpModal)


@pytest.mark.asyncio
async def test_sort_and_status_filter_cycle(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.press("2")  # sort by name
        assert app.sort_key == "name"
        await pilot.press("2")
        assert app.sort_reverse is True

        await pilot.press("f")
        assert app.status_filter == "downloading"
        await pilot.press("escape")  # clears filters
        assert app.status_filter == "all"


@pytest.mark.asyncio
async def test_queue_move_from_the_keyboard(app, torrent_added, tmp_path):
    src = tmp_path / "second"
    src.mkdir()
    _, meta = make_torrent(src, name="second.bin")
    file = tmp_path / "second.torrent"
    file.write_bytes(meta)
    with ipc.Client() as client:
        client.call("add", uri=str(file), paused=True)

    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        target = app.view_rows[0]["hash"]  # cursor starts on the first row

        def position() -> int:
            return next(t["queue"] for t in app.torrents if t["hash"] == target)

        before = position()
        await pilot.press("shift+down")
        for _ in range(25):
            await pilot.pause(0.2)
            await app.refresh_data()
            if position() != before:
                break
        assert position() == before + 1


@pytest.mark.asyncio
async def test_details_panel_toggle(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        visible_before = app._details_visible
        await pilot.press("i")
        await pilot.pause()
        assert app._details_visible is (not visible_before)


@pytest.mark.asyncio
async def test_the_daemon_socket_is_released_on_exit(app):
    """The daemon outlives the UI on purpose; its socket must not."""
    async with app.run_test(size=(120, 35)):
        await app.refresh_data()
        assert app.client.connected
    assert not app.client.connected


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_untouched_limit_does_not_drift(app):
    """The prefill is rounded for humans ("1.2 MiB"). Parsing it back on
    Apply would quietly change a limit the user never touched — every time.
    """
    from torsh.tui.modals import LimitsModal

    exact = 1_234_567
    async with app.run_test(size=(120, 35)) as pilot:
        applied: list[object] = []
        app.push_screen(LimitsModal("Limits", exact, 0), applied.append)
        await pilot.pause()
        await pilot.press("enter")  # Apply, without editing either field
        await pilot.pause()
    assert applied == [(exact, 0)]


@pytest.mark.asyncio
async def test_an_edited_limit_is_parsed_and_garbage_is_refused(app):
    from textual.widgets import Input, Static

    from torsh.tui.modals import LimitsModal

    async with app.run_test(size=(120, 35)) as pilot:
        applied: list[object] = []
        app.push_screen(LimitsModal("Limits", 0, 0), applied.append)
        await pilot.pause()
        modal = app.screen
        modal.query_one("#down", Input).value = "12 parrots"
        await pilot.press("enter")
        await pilot.pause()
        assert applied == [], "a dialog that closes on bad input loses the edit"
        # Textual renamed Static.renderable → .content mid-range; torsh
        # supports both, so the test must read whichever exists.
        error_box = modal.query_one("#limit-error", Static)
        shown = getattr(error_box, "content", None) or getattr(error_box, "renderable", "")
        assert "parrots" in str(shown)

        modal.query_one("#down", Input).value = "2M"
        await pilot.press("enter")
        await pilot.pause()
    assert applied == [(2 * 1024**2, 0)]


@pytest.mark.asyncio
async def test_priorities_modal_collects_changes_as_you_go(app):
    from torsh.tui.modals import PrioritiesModal

    files = [
        {"index": 0, "path": "disc/movie.mkv", "size": 10, "progress": 0.0, "priority_label": "normal"},
        {"index": 1, "path": "disc/sample.mkv", "size": 10, "progress": 0.0, "priority_label": "normal"},
    ]
    async with app.run_test(size=(120, 35)) as pilot:
        applied: list[object] = []
        app.push_screen(PrioritiesModal("ubuntu.iso", files), applied.append)
        await pilot.pause()
        await pilot.press("s")  # skip the first file; the cursor advances
        await pilot.press("h")  # high on the second
        await pilot.press("enter")  # apply
        await pilot.pause()
    assert applied == [{"0": "skip", "1": "high"}]


@pytest.mark.asyncio
async def test_remove_modal_defaults_to_keeping_the_files(app):
    """Destructive by accident is not an option: Cancel is focused and the
    "keep" radio is preselected."""
    from torsh.tui.modals import RemoveModal

    async with app.run_test(size=(120, 35)) as pilot:
        chosen: list[object] = []
        app.push_screen(RemoveModal(["ubuntu.iso", "fedora.iso"]), chosen.append)
        await pilot.pause()
        assert app.screen.query_one("#cancel").has_focus
        await pilot.press("enter")  # activates the focused Cancel button
        await pilot.pause()
    assert chosen == [None]


# ---------------------------------------------------------------------------
# Actions that reach the daemon
# ---------------------------------------------------------------------------


async def _settle(pilot, app, predicate, tries: int = 30):
    """Poll the daemon until a change lands (its cache refreshes at ~1 Hz)."""
    for _ in range(tries):
        await pilot.pause(0.2)
        await app.refresh_data()
        if predicate():
            return True
    return False


@pytest.mark.asyncio
async def test_removing_from_the_table_asks_first_and_then_removes(app, torrent_added):
    from torsh.tui.modals import RemoveModal

    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        assert len(app.torrents) == 1

        await pilot.press("d")
        await pilot.pause(0.3)
        assert isinstance(app.screen, RemoveModal), "removal must be confirmed"

        app.screen.dismiss("keep")  # keep the files, drop the torrent
        assert await _settle(pilot, app, lambda: app.torrents == [])


@pytest.mark.asyncio
async def test_sequential_toggles_for_the_row_under_the_cursor(app, torrent_added):
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        assert app.torrents[0]["sequential"] is False

        await pilot.press("s")
        assert await _settle(pilot, app, lambda: app.torrents[0]["sequential"] is True)

        await pilot.press("s")
        assert await _settle(pilot, app, lambda: app.torrents[0]["sequential"] is False)


@pytest.mark.asyncio
async def test_global_limits_round_trip_through_the_daemon(app):
    from torsh.tui.modals import LimitsModal

    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        app.action_global_limits()
        await pilot.pause(0.3)
        assert isinstance(app.screen, LimitsModal)

        app.screen.dismiss((2 * 1024**2, 512 * 1024))
        assert await _settle(
            pilot, app, lambda: app.session.get("limits") == {"down": 2 * 1024**2, "up": 512 * 1024}
        )

    with ipc.Client() as client:  # ...and it is the daemon that now believes it
        assert client.call("global_limits") == {"down": 2 * 1024**2, "up": 512 * 1024}


@pytest.mark.asyncio
async def test_a_vanishing_daemon_is_reported_not_fatal(app, torrent_added):
    """The daemon can restart under the UI; the UI must survive and recover."""
    async with app.run_test(size=(120, 35)) as pilot:
        await app.refresh_data()
        await pilot.pause()
        assert app.connected is True

        with pytest.MonkeyPatch.context() as patch:

            async def unreachable(*args, **kwargs):
                raise ipc.DaemonUnavailable("daemon went away")

            patch.setattr(app.client, "call", unreachable)
            await app.refresh_data()
            assert app.connected is False

        await app.refresh_data()  # the real client is back
        assert app.connected is True


def test_absolute_dir_anchors_to_the_user_not_the_daemon(tmp_path, monkeypatch):
    from torsh.tui.app import _absolute_dir

    assert _absolute_dir("") is None
    monkeypatch.chdir(tmp_path)
    assert _absolute_dir("isos") == str(tmp_path / "isos")
    assert _absolute_dir("~/isos") == str(Path.home() / "isos")
