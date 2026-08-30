"""CLI contract tests: a real daemon behind real commands via CliRunner."""

from __future__ import annotations

import json
import time

import pytest
from click.testing import CliRunner

from tests.test_daemon import running_daemon  # noqa: F401 - fixture reuse
from tests.test_engine import PIECE, make_torrent
from torsh.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded(running_daemon, tmp_path, runner):  # noqa: F811 - fixture chain
    """A daemon with one paused torrent added through the CLI."""
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    torrent_file = tmp_path / "cli.torrent"
    torrent_file.write_bytes(meta)
    result = runner.invoke(main, ["add", str(torrent_file), "--paused"])
    assert result.exit_code == 0, result.output
    return torrent_file


def test_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "torsh" in result.output


def test_cli_import_stays_light():
    """The client must never pay for the engine it doesn't run.

    libtorrent (C++ extension), the daemon, asyncio, textual and the TOML
    writer are all invisible to `torsh ls`-style invocations — this is what
    keeps cold CLI startup fast.
    """
    import subprocess
    import sys as _sys

    probe = (
        "import sys, torsh.cli\n"
        "heavy = [m for m in ('libtorrent', 'torsh.engine', 'torsh.daemon',"
        " 'asyncio', 'textual', 'tomli_w') if m in sys.modules]\n"
        "sys.exit('leaked: ' + ', '.join(heavy) if heavy else 0)\n"
    )
    result = subprocess.run([_sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_cli_vocabularies_match_engine():
    """The CLI keeps its own copies of the vocabularies — no drift allowed."""
    from torsh.cli.torrents import PRIORITY_WORDS, QUEUE_MOVES
    from torsh.engine import PRIORITY_NAMES
    from torsh.engine import QUEUE_MOVES as ENGINE_QUEUE_MOVES

    assert set(PRIORITY_WORDS) == set(PRIORITY_NAMES)
    assert set(QUEUE_MOVES) == set(ENGINE_QUEUE_MOVES)


def test_help_shows_groups(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for word in ("add", "ls", "daemon", "doctor"):
        assert word in result.output


def test_add_and_ls_json(seeded, runner):
    result = runner.invoke(main, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    torrents = json.loads(result.output)
    assert len(torrents) == 1
    assert torrents[0]["name"] == "payload.bin"
    assert torrents[0]["status"] in {"paused", "checking"}


def test_ls_filters(seeded, runner):
    assert "payload" in runner.invoke(main, ["ls", "payload"]).output
    result = runner.invoke(main, ["ls", "zzz-no-match", "--json"])
    assert json.loads(result.output) == []
    result = runner.invoke(main, ["ls", "--status", "downloading", "--json"])
    assert json.loads(result.output) == []


def test_info_files_trackers(seeded, runner):
    result = runner.invoke(main, ["info", "1"])
    assert result.exit_code == 0
    assert "payload.bin" in result.output

    result = runner.invoke(main, ["files", "payload", "--json"])
    files = json.loads(result.output)
    assert files[0]["path"] == "payload.bin"

    result = runner.invoke(main, ["trackers", "1"])
    assert result.exit_code == 0  # "no trackers" warning is fine


def test_pause_resume_by_selector(seeded, runner):
    result = runner.invoke(main, ["resume", "all"])
    assert result.exit_code == 0
    assert "resumed 1" in result.output

    result = runner.invoke(main, ["pause", "payload"])
    assert result.exit_code == 0
    assert "paused 1" in result.output


def test_bad_selector_fails_cleanly(seeded, runner):
    result = runner.invoke(main, ["pause", "nothing-like-this"])
    assert result.exit_code == 1
    assert "nothing matches" in result.output


def test_prio_and_seq(seeded, runner):
    result = runner.invoke(main, ["prio", "1", "0=high"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["seq", "1", "on"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["seq", "1"])
    assert "on" in result.output


def test_queue_shows_and_moves(seeded, runner):
    result = runner.invoke(main, ["queue", "1"])
    assert result.exit_code == 0, result.output
    assert "queue position" in result.output

    result = runner.invoke(main, ["queue", "1", "top"])
    assert result.exit_code == 0, result.output
    assert "queue position 0" in result.output

    result = runner.invoke(main, ["queue", "1", "sideways"])
    assert result.exit_code == 2  # click rejects the choice before any RPC


def test_export_writes_a_torrent_file(seeded, runner, tmp_path):
    out = tmp_path / "exports"
    result = runner.invoke(main, ["export", "1", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "payload.bin.torrent").exists()


def test_add_reads_uris_from_stdin(running_daemon, runner, tmp_path):  # noqa: F811
    src = tmp_path / "src"
    src.mkdir()
    _, meta = make_torrent(src)
    torrent_file = tmp_path / "piped.torrent"
    torrent_file.write_bytes(meta)

    result = runner.invoke(main, ["add", "-", "--paused"], input=f"{torrent_file}\n\n")
    assert result.exit_code == 0, result.output
    assert "added" in result.output
    assert len(json.loads(runner.invoke(main, ["ls", "--json"]).output)) == 1


def test_speed_and_limit(seeded, runner):
    result = runner.invoke(main, ["speed", "2M", "500K"])
    assert result.exit_code == 0
    assert "2.0 MiB/s" in result.output

    result = runner.invoke(main, ["limit", "1", "1M"])
    assert result.exit_code == 0
    assert "1.0 MiB/s" in result.output

    result = runner.invoke(main, ["speed", "off", "off"])
    assert "∞" in result.output


def test_rm_requires_confirmation(seeded, runner):
    result = runner.invoke(main, ["rm", "1"], input="n\n")
    assert result.exit_code == 0
    assert "aborted" in result.output

    result = runner.invoke(main, ["rm", "1", "--yes"])
    assert result.exit_code == 0
    assert "removed 1" in result.output


def test_stats_json(running_daemon, runner):  # noqa: F811
    result = runner.invoke(main, ["stats", "--json"])
    payload = json.loads(result.output)
    assert payload["engine"].startswith("libtorrent")


def test_daemon_status_json(running_daemon, runner):  # noqa: F811
    result = runner.invoke(main, ["daemon", "status", "--json"])
    payload = json.loads(result.output)
    assert payload["running"] is True


def test_config_roundtrip_via_cli(running_daemon, runner):  # noqa: F811
    result = runner.invoke(main, ["config", "set", "limits.download", "3M"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["config", "get", "limits.download"])
    assert result.output.strip() == str(3 * 1024**2)

    result = runner.invoke(main, ["config", "set", "nope.nope", "1"])
    assert result.exit_code == 1

    result = runner.invoke(main, ["config", "list"])
    assert "limits.download" in result.output


def test_doctor_without_daemon(runner, tmp_path, monkeypatch):
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "libtorrent" in result.output


def test_doctor_fails_on_an_unreadable_config(runner):
    """Defaults are a fine fallback, but the user has to be told why."""
    from torsh import paths

    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("this is not [ toml", encoding="utf-8")
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "not valid TOML" in result.output


def test_doctor_flags_a_daemon_left_behind_by_an_upgrade(running_daemon, runner, monkeypatch):  # noqa: F811
    """After `pip install -U`, the *old* daemon keeps serving every command."""
    monkeypatch.setattr("torsh.cli.system.__version__", "99.0.0")
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "daemon restart" in result.output


def test_daemon_status_exits_3_when_nothing_is_running(runner):
    """A distinct code so scripts can tell "down" from "broken"."""
    result = runner.invoke(main, ["daemon", "status"])
    assert result.exit_code == 3
    assert json.loads(runner.invoke(main, ["daemon", "status", "--json"]).output) == {"running": False}


def test_move_relocates_a_torrent(seeded, runner, tmp_path):
    target = tmp_path / "elsewhere"
    result = runner.invoke(main, ["move", "1", str(target)])
    assert result.exit_code == 0, result.output
    assert "moving" in result.output
    # The path is echoed back (rich may wrap it across lines on a narrow tty).
    assert "elsewhere" in result.output.replace("\n", "")

    # libtorrent moves storage asynchronously (the torrent reads "moving"
    # meanwhile), so the new path shows up on a later status update.
    def save_path() -> str:
        return json.loads(runner.invoke(main, ["ls", "--json"]).output)[0]["save_path"]

    for _ in range(50):
        if save_path() == str(target):
            break
        time.sleep(0.2)
    assert save_path() == str(target)


def test_info_json_and_peers(seeded, runner):
    detail = json.loads(runner.invoke(main, ["info", "1", "--json"]).output)
    assert detail["piece_count"] > 0
    assert detail["file_count"] == 1
    assert runner.invoke(main, ["peers", "1"]).exit_code == 0  # "no peers" is fine


def test_ls_sorting_is_stable_and_reversible(seeded, runner):
    for order in ("name", "size", "eta", "ratio", "progress", "added"):
        result = runner.invoke(main, ["ls", "--sort", order, "--json"])
        assert result.exit_code == 0, f"--sort {order}: {result.output}"
        assert len(json.loads(result.output)) == 1
    assert runner.invoke(main, ["ls", "--sort", "nope"]).exit_code == 2


def test_status_choices_match_the_selector_groups():
    """`--status paused` and the bare selector `paused` are the same idea;
    keeping two lists would let one grow a filter the other lacks."""
    from torsh.cli.torrents import STATUS_CHOICES
    from torsh.select import GROUPS

    assert list(GROUPS) == STATUS_CHOICES


def test_python_m_torsh_is_the_same_entry_point():
    """`python -m torsh` has to work even where the script isn't on PATH."""
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-m", "torsh", "--version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "torsh" in result.stdout


# ---------------------------------------------------------------------------
# get — the foreground, wget-style download
# ---------------------------------------------------------------------------


def test_get_attaches_to_an_existing_torrent_and_reports_completion(running_daemon, runner, tmp_path):  # noqa: F811
    """`torsh get` on something already finished must print the path and
    exit, not sit there watching a completed download forever."""
    payload_dir = tmp_path / "already-here"
    payload_dir.mkdir()
    _, meta = make_torrent(payload_dir, size=PIECE * 4)
    torrent_file = tmp_path / "done.torrent"
    torrent_file.write_bytes(meta)

    # Pointing the save path at the existing data makes libtorrent verify it
    # and go straight to seeding — a finished torrent, without a transfer.
    result = runner.invoke(main, ["get", str(torrent_file), "-d", str(payload_dir)])
    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert "payload.bin" in result.output.replace("\n", "")

    # ...and running it again attaches to the duplicate instead of failing.
    again = runner.invoke(main, ["get", str(torrent_file), "-d", str(payload_dir)])
    assert again.exit_code == 0, again.output
    assert "already added" in again.output


def test_ls_watch_renders_a_frame_then_detaches(seeded, runner, monkeypatch):
    """The live view is a loop around one frame; Ctrl+C leaves downloads be.

    The interrupt is aimed at Live.update rather than `time.sleep`, which is
    process-global and would fire inside rich's own refresh thread too.
    """

    def interrupt(self, renderable=None, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("rich.live.Live.update", interrupt)
    result = runner.invoke(main, ["ls", "--watch"])
    assert result.exit_code == 0, result.output
    assert "payload.bin" in result.output.replace("\n", "")
    assert "Ctrl+C" in result.output  # the frame carries its own exit hint


# ---------------------------------------------------------------------------
# daemon & config subcommands
# ---------------------------------------------------------------------------


def test_daemon_start_is_idempotent(running_daemon, runner):  # noqa: F811
    result = runner.invoke(main, ["daemon", "start"])
    assert result.exit_code == 0
    assert "already running" in result.output


def test_daemon_log_shows_the_tail_or_says_there_is_none(runner):
    from torsh import paths

    assert "no log yet" in runner.invoke(main, ["daemon", "log"]).output

    logfile = paths.daemon_log_file()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.write_text("\n".join(f"entry {n}" for n in range(50)), encoding="utf-8")

    result = runner.invoke(main, ["daemon", "log", "-n", "3"])
    assert result.exit_code == 0
    assert "entry 49" in result.output
    assert "entry 46" not in result.output, "-n must actually bound the output"


def test_daemon_stop_is_idempotent(running_daemon, runner):  # noqa: F811
    stopped = runner.invoke(main, ["daemon", "stop"])
    assert stopped.exit_code == 0, stopped.output
    assert "daemon stopped" in stopped.output
    assert "not running" in runner.invoke(main, ["daemon", "stop"]).output


def test_config_path_and_edit(runner, monkeypatch):
    from torsh import paths

    path_result = runner.invoke(main, ["config", "path"])
    assert path_result.output.strip() == str(paths.config_file())

    # `true` accepts any arguments and exits 0 — a no-op stand-in for $EDITOR.
    monkeypatch.setenv("EDITOR", "true")
    assert runner.invoke(main, ["config", "edit"]).exit_code == 0
    assert paths.config_file().exists(), "edit must materialise the file first"


def test_config_list_renders_rates_readably(runner):
    runner.invoke(main, ["config", "set", "limits.download", "2M"])
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0
    assert "2.0 MiB/s" in result.output
    assert "unlimited" in result.output  # upload is still 0


# ---------------------------------------------------------------------------
# Error plumbing
# ---------------------------------------------------------------------------


def test_a_closed_pipe_exits_like_sigpipe(monkeypatch):
    """`torsh ls --json | head -1`: the reader hangs up mid-write.

    Python's parting "Exception ignored in: <_io.TextIOWrapper…>" would land
    on the user's terminal long after their command looked successful.
    """
    from torsh.cli import common

    redirected: list[tuple[int, int]] = []
    monkeypatch.setattr(common.os, "open", lambda *a, **k: 4242)
    monkeypatch.setattr(common.os, "dup2", lambda src, dst: redirected.append((src, dst)))

    @common.handle_errors
    def command() -> None:
        raise BrokenPipeError(32, "Broken pipe")

    with pytest.raises(SystemExit) as excinfo:
        command()
    assert excinfo.value.code == 141  # 128 + SIGPIPE, what a shell reports
    assert redirected and redirected[0][0] == 4242


def test_an_environment_error_is_one_line_not_a_traceback(runner):
    """A foreign runtime dir or a read-only home is the user's problem —
    they should read a sentence, not a stack."""
    from torsh.cli import common

    @common.handle_errors
    def command() -> None:
        raise PermissionError(13, "Permission denied", "/somewhere/torsh-0")

    with pytest.raises(SystemExit) as excinfo:
        command()
    assert excinfo.value.code == 1


def test_interrupting_a_command_exits_130():
    from torsh.cli import common

    @common.handle_errors
    def command() -> None:
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as excinfo:
        command()
    assert excinfo.value.code == 130
