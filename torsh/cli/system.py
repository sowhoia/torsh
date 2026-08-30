"""System commands: daemon lifecycle, configuration, diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import rich_click as click
from rich.text import Text

from .. import __version__, ipc, paths
from .. import config as config_mod
from ..units import fmt_duration, fmt_size
from . import render
from .common import handle_errors

# NOTE: `torsh.daemon` (and through it libtorrent) is imported inside the
# daemon subcommands only — `torsh ls`/`--help` must stay import-light.

# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


@click.group()
def daemon() -> None:
    """Manage the background daemon that runs your torrents."""


@daemon.command(name="run")
@click.option("--quiet", is_flag=True, help="No startup banner (for supervisors).")
@handle_errors
def daemon_run(quiet: bool) -> None:
    """Run the daemon in the foreground (systemd/launchd friendly)."""
    from .. import daemon as daemon_mod

    if not quiet:
        render.econsole().print(f"[bold {render.CYAN}]torsh daemon[/] v{__version__} — Ctrl+C to stop")
    daemon_mod.run_daemon(config_mod.load())


@daemon.command(name="start")
@handle_errors
def daemon_start() -> None:
    """Start the daemon in the background."""
    if ipc.daemon_running():
        render.warn("daemon is already running")
        return
    client = ipc.ensure_daemon(autostart=True)
    pong = client.call("ping")
    client.close()
    render.ok(f"daemon started [dim](pid {pong['pid']})[/]")


@daemon.command(name="stop")
@handle_errors
def daemon_stop() -> None:
    """Stop the daemon (saves resume data for every torrent)."""
    from .. import daemon as daemon_mod

    if not ipc.daemon_running():
        render.warn("daemon is not running")
        return
    if daemon_mod.stop_daemon():
        render.ok("daemon stopped")
    else:
        render.fail("daemon did not stop in time — check `torsh daemon log`")
        sys.exit(1)


@daemon.command(name="restart")
@handle_errors
def daemon_restart() -> None:
    """Stop and start the daemon."""
    from .. import daemon as daemon_mod

    if ipc.daemon_running() and not daemon_mod.stop_daemon():
        render.fail("daemon did not stop in time")
        sys.exit(1)
    client = ipc.ensure_daemon(autostart=True)
    pong = client.call("ping")
    client.close()
    render.ok(f"daemon restarted [dim](pid {pong['pid']})[/]")


@daemon.command(name="status")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def daemon_status(as_json: bool) -> None:
    """Is the daemon alive, and what is it doing?"""
    if not ipc.daemon_running():
        if as_json:
            render.emit_json({"running": False})
        else:
            render.warn("daemon is not running — `torsh daemon start`")
        sys.exit(3)
    with ipc.Client() as client:
        pong = client.call("ping")
        session = client.call("session")
    if as_json:
        render.emit_json({"running": True, **pong, "session": session})
        return
    rows = [
        ("state", Text("● running", style=render.GREEN)),
        ("pid", str(pong["pid"])),
        ("uptime", fmt_duration(pong["uptime"], compact=False)),
        ("torrents", f"{session['torrents']} ({session['active']} active)"),
        ("socket", str(paths.socket_path())),
        ("version", pong["version"]),
    ]
    render.console().print(render.kv_panel("daemon", rows))


@daemon.command(name="log")
@click.option("--follow", "-f", is_flag=True, help="Keep printing as new lines arrive.")
@click.option("--lines", "-n", default=40, show_default=True, help="How many lines to show.")
@handle_errors
def daemon_log(follow: bool, lines: int) -> None:
    """Show the daemon's log."""
    logfile = paths.daemon_log_file()
    if not logfile.exists():
        render.warn(f"no log yet at {logfile}")
        return
    if follow:
        subprocess.run(["tail", "-n", str(lines), "-f", str(logfile)], check=False)
        return
    content = logfile.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        render.console().print(line, markup=False, highlight=False)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@click.group(name="config")
def config_group() -> None:
    """Read and edit torsh's configuration."""


def _reload_daemon_if_running() -> None:
    if ipc.daemon_running():
        with ipc.Client() as client:
            client.call("reload")
        render.ok("daemon reloaded")


@config_group.command(name="list")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def config_list(as_json: bool) -> None:
    """Show every setting and its current value."""
    config = config_mod.load()
    pairs = config_mod.list_keys(config)
    if as_json:
        render.emit_json(dict(pairs))
        return
    rows = [(key, _pretty_value(key, value)) for key, value in pairs]
    render.console().print(render.kv_panel(str(paths.config_file()), rows))


def _pretty_value(key: str, value: object) -> str:
    if key in {"limits.download", "limits.upload"} and isinstance(value, int):
        return f"{value} ({fmt_size(value)}/s)" if value else "0 (unlimited)"
    return repr(value) if isinstance(value, str) else str(value)


@config_group.command(name="get")
@click.argument("key")
@handle_errors
def config_get(key: str) -> None:
    """Print one setting, e.g. `torsh config get limits.download`."""
    config = config_mod.load()
    try:
        print(config_mod.get_key(config, key))
    except KeyError:
        render.fail(f"unknown key {key!r} — see `torsh config list`")
        sys.exit(1)


@config_group.command(name="set")
@click.argument("key")
@click.argument("value")
@handle_errors
def config_set(key: str, value: str) -> None:
    """Change a setting, e.g. `torsh config set limits.download 2M`.

    The running daemon picks the change up immediately.
    """
    # env=False: editing the file must not bake session TORSH_* overrides in.
    config = config_mod.load(env=False)
    try:
        applied = config_mod.set_key(config, key, value)
    except KeyError:
        render.fail(f"unknown key {key!r} — see `torsh config list`")
        sys.exit(1)
    except ValueError as exc:
        render.fail(str(exc))
        sys.exit(1)
    config_mod.save(config)
    render.ok(f"{key} = {applied}")
    _reload_daemon_if_running()


@config_group.command(name="path")
@handle_errors
def config_path() -> None:
    """Print the config file location."""
    print(paths.config_file())


@config_group.command(name="edit")
@handle_errors
def config_edit() -> None:
    """Open the config in $EDITOR, then reload the daemon."""
    config_mod.load()  # materialise the file with defaults first
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    result = subprocess.run([editor, str(paths.config_file())], check=False)
    if result.returncode == 0:
        _reload_daemon_if_running()


# ---------------------------------------------------------------------------
# doctor / completion
# ---------------------------------------------------------------------------


_LOW_DISK = 2 * 1024**3


class _Report:
    """Doctor's running tally: findings go straight to the terminal, and any
    ``fail`` makes the whole command exit non-zero for scripts to notice."""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, message: str) -> None:
        render.ok(message)

    def warn(self, message: str) -> None:
        render.warn(message)

    def fail(self, message: str) -> None:
        self.failures += 1
        render.fail(message)


def _check_runtime(report: _Report, config: config_mod.Config) -> None:
    report.ok(f"torsh {__version__} on Python {sys.version.split()[0]}")
    try:
        import libtorrent as lt

        report.ok(f"libtorrent {lt.version}")
    except ImportError as exc:  # pragma: no cover - a broken wheel, not a code path
        report.fail(f"libtorrent import failed: {exc}")


def _check_config(report: _Report, config: config_mod.Config) -> None:
    broken = config_mod.parse_error()
    if broken:
        # load() falls back to defaults silently, so without this the user
        # just sees their settings "not working" with nothing to point at.
        report.fail(f"config at {paths.config_file()} is not valid TOML — running on defaults ({broken})")
    else:
        report.ok(f"config at {paths.config_file()}")


def _check_state(report: _Report, config: config_mod.Config) -> None:
    try:
        paths.ensure_dirs()
    except OSError as exc:
        report.fail(f"state dir {paths.state_dir()} unusable: {exc}")
    else:
        report.ok(f"state at {paths.state_dir()}")


def _check_downloads(report: _Report, config: config_mod.Config) -> None:
    download_dir = config.downloads.resolved_dir()
    try:
        download_dir.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(download_dir)
    except OSError as exc:
        report.fail(f"downloads dir {download_dir} not writable: {exc}")
        return
    line = f"downloads → {download_dir} ({fmt_size(usage.free)} free)"
    if usage.free < _LOW_DISK:
        report.warn(f"{line} — less than {fmt_size(_LOW_DISK)} free")
    else:
        report.ok(line)


def _check_daemon(report: _Report, config: config_mod.Config) -> None:
    if not ipc.daemon_running():
        report.ok("daemon not running (will autostart on demand)")
        return
    with ipc.Client() as client:
        pong = client.call("ping")
        session = client.call("session")
    report.ok(f"daemon running — {session['torrents']} torrent(s), port {session['listen_port']}")
    if pong["version"] != __version__:
        # A pip upgrade leaves the *old* daemon running, so every command
        # keeps talking to yesterday's engine — baffling in the wild.
        report.warn(
            f"daemon is running torsh {pong['version']}, this CLI is {__version__}"
            " — `torsh daemon restart` to pick up the upgrade"
        )
    if not config.network.dht:
        report.ok("DHT disabled by config")
    elif session["dht_nodes"] > 0:
        report.ok(f"DHT: {session['dht_nodes']} nodes")
    else:
        report.warn("DHT: 0 nodes (still bootstrapping?) — magnets may resolve slowly")


# Order matters: cheapest and most fundamental first, so the output reads as
# a narrowing diagnosis rather than a bag of unrelated lines.
_CHECKS = (_check_runtime, _check_config, _check_state, _check_downloads, _check_daemon)


@click.command()
@handle_errors
def doctor() -> None:
    """Check that everything torsh needs is in order."""
    report = _Report()
    config = config_mod.load()
    for check in _CHECKS:
        check(report, config)
    if report.failures:
        sys.exit(1)


_COMPLETION_HOWTO = {
    "bash": 'eval "$(_TORSH_COMPLETE=bash_source torsh)"   # add to ~/.bashrc',
    "zsh": 'eval "$(_TORSH_COMPLETE=zsh_source torsh)"    # add to ~/.zshrc',
    "fish": "_TORSH_COMPLETE=fish_source torsh | source    # add to ~/.config/fish/config.fish",
}


@click.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@handle_errors
def completion(shell: str) -> None:
    """Print the shell-completion script for bash, zsh or fish."""
    env = os.environ.copy()
    env["_TORSH_COMPLETE"] = f"{shell}_source"
    executable = shutil.which("torsh") or sys.argv[0]
    result = subprocess.run([executable], env=env, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        print(result.stdout, end="")
        render.econsole().print(f"[dim]# activate: {_COMPLETION_HOWTO[shell]}[/]")
    else:  # pragma: no cover - depends on install layout
        render.warn("couldn't generate the script directly; use:")
        print(_COMPLETION_HOWTO[shell])
