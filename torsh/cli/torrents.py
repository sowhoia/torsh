"""Torrent commands: the day-to-day verbs."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import rich_click as click

from ..select import GROUPS, SORT_KEYS, SelectorError
from ..units import fmt_limit, fmt_size, parse_size
from . import render
from .common import client, handle_errors, hashes, pick, summarize

# Straight from the selector registry — `--status` and a bare selector word
# must never drift apart.
STATUS_CHOICES = list(GROUPS)

# Mirror torsh.engine's vocabularies — kept local so `torsh prio --help`
# never imports the engine module (and with it the libtorrent extension).
# tests/test_cli.py asserts both stay in sync with the engine.
PRIORITY_WORDS = ("skip", "low", "normal", "high")
QUEUE_MOVES = ("up", "down", "top", "bottom")


def _expand_uri(uri: str) -> str:
    """Local paths become absolute — the daemon has its own cwd."""
    if uri.startswith(("magnet:", "http://", "https://")):
        return uri
    path = Path(uri).expanduser()
    if path.exists():
        return str(path.resolve())
    return uri


def _expand_dir(directory: str | None) -> str | None:
    """`-d isos` means ./isos from the user's shell, not the daemon's cwd."""
    if directory is None:
        return None
    return str(Path(directory).expanduser().resolve())


def _read_stdin_uris() -> list[str]:
    """`torsh add -` — one URI per line, so `cat magnets.txt | torsh add -` works."""
    if sys.stdin is None or sys.stdin.isatty():
        raise SelectorError("`-` reads URIs from stdin, but stdin is a terminal")
    return [line.strip() for line in sys.stdin if line.strip()]


def _filter_torrents(torrents: list[dict[str, Any]], query: str | None, status: str) -> list[dict[str, Any]]:
    result = torrents
    if status != "all":
        result = [t for t in result if GROUPS[status](t)]
    if query:
        needle = query.lower()
        result = [t for t in result if needle in t["name"].lower()]
    return result


# ---------------------------------------------------------------------------
# add / ls / get
# ---------------------------------------------------------------------------


@click.command()
@click.argument("uris", nargs=-1, required=True, metavar="URI...")
@click.option("--dir", "-d", "directory", default=None, help="Download directory for these torrents.")
@click.option("--paused", is_flag=True, help="Add without starting.")
@click.option("--sequential", "-s", is_flag=True, default=None, help="Stream-friendly piece order.")
@click.option("--json", "as_json", is_flag=True, help="Print the added torrents as JSON.")
@handle_errors
def add(
    uris: tuple[str, ...], directory: str | None, paused: bool, sequential: bool | None, as_json: bool
) -> None:
    """Add torrents: magnet links, .torrent files, URLs or infohashes.

    A lone [bold]-[/] reads one URI per line from stdin.

    \b
    Examples:
      torsh add magnet:?xt=urn:btih:…
      torsh add ubuntu.torrent fedora.torrent -d ~/isos
      torsh add https://example.org/arch.torrent --sequential
      cat magnets.txt | torsh add -
    """
    if "-" in uris:
        uris = tuple(u for u in uris if u != "-") + tuple(_read_stdin_uris())
        if not uris:
            raise SelectorError("nothing on stdin to add")
    cli = client()
    directory = _expand_dir(directory)
    added, failed = [], 0
    for uri in uris:
        try:
            view = cli.call("add", uri=_expand_uri(uri), dir=directory, paused=paused, sequential=sequential)
            added.append(view)
            if not as_json:
                extra = " [magenta](sequential)[/]" if view["sequential"] else ""
                size = fmt_size(view["size"]) if view["has_metadata"] else "fetching metadata…"
                render.ok(f"added [bold]{view['name']}[/] [dim]#{view['id']} · {size}[/]" + extra)
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            failed += 1
            render.fail(f"{uri}: {exc}")
    if as_json:
        render.emit_json(added)
    elif len(uris) > 1:
        tally = f"{len(added)} of {len(uris)} added"
        (render.warn if failed else render.ok)(tally)
    if failed:
        sys.exit(1)


@click.command(name="ls")
@click.argument("query", required=False)
@click.option("--status", "-s", type=click.Choice(STATUS_CHOICES), default="all", help="Filter by state.")
@click.option("--sort", "-o", type=click.Choice(list(SORT_KEYS)), default="id", help="Sort column.")
@click.option("--reverse", "-r", is_flag=True, help="Reverse the sort order.")
@click.option("--watch", "-w", is_flag=True, help="Live-updating view (Ctrl+C to exit).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@handle_errors
def ls(query: str | None, status: str, sort: str, reverse: bool, watch: bool, as_json: bool) -> None:
    """List torrents (optionally filtered by a name QUERY).

    \b
    Examples:
      torsh ls
      torsh ls ubuntu --status downloading
      torsh ls --sort down -r --watch
    """
    cli = client()

    def snapshot() -> list[dict[str, Any]]:
        torrents = _filter_torrents(cli.call("list"), query, status)
        return sorted(torrents, key=SORT_KEYS[sort], reverse=reverse)

    if as_json:
        render.emit_json(snapshot())
        return

    if watch:
        from rich.live import Live

        def frame() -> Any:
            snap = cli.call("snapshot")
            torrents = _filter_torrents(snap["torrents"], query, status)
            torrents.sort(key=SORT_KEYS[sort], reverse=reverse)
            return render.watch_view(torrents, snap["session"])

        with Live(frame(), console=render.console(), refresh_per_second=4) as live:
            try:
                while True:
                    time.sleep(1.0)
                    live.update(frame())
            except KeyboardInterrupt:
                pass
        return

    torrents = snapshot()
    if not torrents:
        filtered = bool(query or status != "all")
        render.warn("no torrents match" if filtered else "no torrents yet — `torsh add <uri>`")
        return
    render.console().print(render.torrent_table(torrents))


@click.command()
@click.argument("uri")
@click.option("--dir", "-d", "directory", default=None, help="Download directory.")
@click.option("--sequential", "-s", is_flag=True, default=None, help="Stream-friendly piece order.")
@handle_errors
def get(uri: str, directory: str | None, sequential: bool | None) -> None:
    """Download one torrent in the foreground and exit when it completes.

    Ctrl+C leaves the download running in the daemon (`torsh ls` to check).

    \b
    Examples:
      torsh get magnet:?xt=urn:btih:…
      torsh get ubuntu.torrent -d ~/isos --sequential
    """
    cli = client()
    try:
        view = cli.call("add", uri=_expand_uri(uri), dir=_expand_dir(directory), sequential=sequential)
        render.ok(f"added [bold]{view['name']}[/]")
    except Exception as exc:
        # Attach to the already-known torrent instead of failing: the
        # duplicate error carries the colliding infohash.
        is_duplicate = getattr(exc, "code", "") == "duplicate"
        existing_hash = getattr(exc, "data", {}).get("hash") if is_duplicate else None
        if not existing_hash:
            raise
        view = cli.call("get", hash=existing_hash)
        render.warn(f"already added — watching [bold]{view['name']}[/]")

    info_hash = view["hash"]
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    progress = Progress(
        SpinnerColumn(style=render.CYAN),
        TextColumn("[progress.description]{task.description}", justify="left"),
        BarColumn(bar_width=30, complete_style=render.CYAN, finished_style=render.GREEN),
        TextColumn("{task.percentage:>5.1f}%"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=render.econsole(),
        transient=False,
    )
    name = view["name"]
    with progress:
        task = progress.add_task(name[:36], total=view["size"] or None)
        try:
            while True:
                current = cli.call("get", hash=info_hash)
                label = current["name"][:36]
                if not current["has_metadata"]:
                    label = f"{label} [dim](metadata…)[/]"
                progress.update(
                    task,
                    total=current["size"] or None,
                    completed=current["done"],
                    description=label,
                )
                if current["has_metadata"] and current["progress"] >= 1.0:
                    break
                if current["status"] == "error" and current["error"]:
                    progress.stop()
                    render.fail(current["error"])
                    sys.exit(1)
                time.sleep(0.5)
        except KeyboardInterrupt:
            progress.stop()
            render.warn("detached — the download continues in the background (`torsh ls`)")
            sys.exit(130)

    final = cli.call("get", hash=info_hash)
    render.ok(f"done → [bold]{Path(final['save_path']) / final['name']}[/]")


# ---------------------------------------------------------------------------
# info / files / peers / trackers
# ---------------------------------------------------------------------------


@click.command()
@click.argument("selector")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def info(selector: str, as_json: bool) -> None:
    """Everything about one torrent (accepts id, name or hash)."""
    cli = client()
    torrent = pick(cli, (selector,))[0]
    view = cli.call("get", hash=torrent["hash"])
    if as_json:
        render.emit_json(view)
        return
    render.console().print(render.detail_panel(view))


@click.command()
@click.argument("selector")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def files(selector: str, as_json: bool) -> None:
    """List a torrent's files with progress and priorities."""
    cli = client()
    torrent = pick(cli, (selector,))[0]
    file_list = cli.call("files", hash=torrent["hash"])
    if as_json:
        render.emit_json(file_list)
        return
    if not file_list:
        render.warn("metadata not downloaded yet")
        return
    render.console().print(render.files_table(file_list))


@click.command()
@click.argument("selector")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def peers(selector: str, as_json: bool) -> None:
    """Show connected peers for a torrent."""
    cli = client()
    torrent = pick(cli, (selector,))[0]
    peer_list = cli.call("peers", hash=torrent["hash"])
    if as_json:
        render.emit_json(peer_list)
        return
    if not peer_list:
        render.warn("no peers connected")
        return
    render.console().print(render.peers_table(peer_list))


@click.command()
@click.argument("selector")
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def trackers(selector: str, as_json: bool) -> None:
    """Show tracker status for a torrent."""
    cli = client()
    torrent = pick(cli, (selector,))[0]
    tracker_list = cli.call("trackers", hash=torrent["hash"])
    if as_json:
        render.emit_json(tracker_list)
        return
    if not tracker_list:
        render.warn("no trackers (DHT/PEX only)")
        return
    render.console().print(render.trackers_table(tracker_list))


# ---------------------------------------------------------------------------
# pause / resume / verify / reannounce / rm / move
# ---------------------------------------------------------------------------


def _bulk_command(name: str, rpc_method: str, done_verb: str, help_text: str):
    @click.command(name=name, help=help_text)
    @click.argument("selectors", nargs=-1, required=True, metavar="SELECTOR...")
    @handle_errors
    def command(selectors: tuple[str, ...]) -> None:
        cli = client()
        picked = pick(cli, selectors)
        count = cli.call(rpc_method, hashes=hashes(picked))
        render.ok(f"{done_verb} {count} torrent(s): {summarize(picked)}")

    return command


pause = _bulk_command("pause", "pause", "paused", "Pause torrents (id, name, hash, or all/active/done).")
resume = _bulk_command("resume", "resume", "resumed", "Resume torrents (id, name, hash, or all/paused).")
verify = _bulk_command("verify", "recheck", "verifying", "Re-check downloaded data against piece hashes.")
reannounce = _bulk_command("reannounce", "reannounce", "reannounced", "Ask trackers and DHT for fresh peers.")


@click.command(name="rm")
@click.argument("selectors", nargs=-1, required=True, metavar="SELECTOR...")
@click.option("--data", "-D", is_flag=True, help="Also delete the downloaded files.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@handle_errors
def rm(selectors: tuple[str, ...], data: bool, yes: bool) -> None:
    """Remove torrents (keeps downloaded data unless --data).

    \b
    Examples:
      torsh rm 3
      torsh rm done -y          # clear out everything finished
      torsh rm ubuntu --data    # delete the files too
    """
    cli = client()
    picked = pick(cli, selectors)
    if not yes:
        stake = "[bold red]and their files[/] " if data else "[dim](files kept)[/] "
        render.econsole().print(f"remove [bold]{len(picked)}[/] torrent(s) {stake}— {summarize(picked, 5)}")
        if not click.confirm("continue?", default=False, err=True):
            render.warn("aborted")
            return
    count = cli.call("remove", hashes=hashes(picked), delete_data=data)
    render.ok(f"removed {count} torrent(s)" + (" with data" if data else ""))


@click.command()
@click.argument("selector")
@click.argument("directory", type=click.Path())
@handle_errors
def move(selector: str, directory: str) -> None:
    """Move a torrent's data to another directory."""
    cli = client()
    torrent = pick(cli, (selector,))[0]
    target = str(Path(directory).expanduser().resolve())
    cli.call("move", hash=torrent["hash"], dir=target)
    render.ok(f"moving [bold]{torrent['name']}[/] → {target}")


@click.command()
@click.argument("selector")
@click.argument("target", required=False, type=click.Path())
@handle_errors
def export(selector: str, target: str | None) -> None:
    """Save a torrent's metadata as a .torrent file (magnets included).

    TARGET is a directory or an explicit path; it defaults to the current
    directory.

    \b
    Examples:
      torsh export ubuntu                  # ./ubuntu-24.04….torrent
      torsh export 3 ~/torrents/
      torsh export 3 ~/torrents/mine.torrent
    """
    cli = client()
    torrent = pick(cli, (selector,))[0]
    destination = str(Path(target or ".").expanduser().resolve())
    written = cli.call("export", hash=torrent["hash"], target=destination)
    render.ok(f"exported [bold]{torrent['name']}[/] → {written}")


@click.command()
@click.argument("selector")
@click.argument("move", type=click.Choice(list(QUEUE_MOVES)), required=False)
@handle_errors
def queue(selector: str, move: str | None) -> None:
    """Show or change a torrent's place in the download queue.

    torsh runs a limited number of torrents at once (see
    [bold]limits.active_downloads[/]); everything else waits as
    [bold]queued[/]. This is how you jump the line.

    \b
    Examples:
      torsh queue ubuntu top
      torsh queue 4 down
      torsh queue 4            # just show the position
    """
    cli = client()
    torrent = pick(cli, (selector,))[0]
    if move is None:
        # -1 is libtorrent's "not queued" — finished torrents leave the queue.
        position = torrent["queue"]
        where = f"queue position {position}" if position >= 0 else "not queued (finished)"
        render.console().print(f"[bold]{torrent['name']}[/] — {where}")
        return
    position = cli.call("queue", hash=torrent["hash"], move=move)
    render.ok(f"[bold]{torrent['name']}[/] moved {move} — queue position {position}")


# ---------------------------------------------------------------------------
# seq / prio / limit / speed / stats
# ---------------------------------------------------------------------------


@click.command()
@click.argument("selector")
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@handle_errors
def seq(selector: str, state: str | None) -> None:
    """Toggle sequential (streaming) download order.

    \b
    Examples:
      torsh seq movie on     # watch while it downloads
      torsh seq movie        # show the current mode
    """
    cli = client()
    torrent = pick(cli, (selector,))[0]
    if state is None:
        mode = "on" if torrent["sequential"] else "off"
        render.console().print(f"sequential: [bold]{mode}[/] — {torrent['name']}")
        return
    cli.call("sequential", hash=torrent["hash"], enabled=state == "on")
    render.ok(f"sequential {state} for [bold]{torrent['name']}[/]")


@click.command()
@click.argument("selector")
@click.argument("changes", nargs=-1, required=True, metavar="INDEX=PRIORITY...")
@handle_errors
def prio(selector: str, changes: tuple[str, ...]) -> None:
    """Set per-file priorities: skip, low, normal or high.

    \b
    Examples:
      torsh prio ubuntu 0=high 3=skip
      torsh prio 2 all=skip 0=normal   # cherry-pick one file
    """
    cli = client()
    torrent = pick(cli, (selector,))[0]
    file_list = cli.call("files", hash=torrent["hash"])
    if not file_list:
        raise SelectorError("metadata not downloaded yet — try again shortly")

    parsed: dict[str, str] = {}
    for change in changes:
        index, _, priority = change.partition("=")
        if not priority or priority.lower() not in PRIORITY_WORDS:
            raise SelectorError(f"bad priority {change!r} — use INDEX=skip|low|normal|high")
        if index.lower() == "all":
            for f in file_list:
                parsed[str(f["index"])] = priority.lower()
        else:
            if not index.isdigit():
                raise SelectorError(f"bad file index in {change!r}")
            parsed[index] = priority.lower()

    cli.call("priorities", hash=torrent["hash"], changes=parsed)
    render.ok(f"updated {len(parsed)} file(s) in [bold]{torrent['name']}[/]")


@click.command()
@click.argument("selector")
@click.argument("down", required=False)
@click.argument("up", required=False)
@handle_errors
def limit(selector: str, down: str | None, up: str | None) -> None:
    """Per-torrent speed limits (human units; 'off' = unlimited).

    \b
    Examples:
      torsh limit ubuntu 2M 500K
      torsh limit ubuntu off off
      torsh limit ubuntu           # show current limits
    """
    cli = client()
    torrent = pick(cli, (selector,))[0]
    down_bytes = parse_size(down) if down is not None else None
    up_bytes = parse_size(up) if up is not None else None
    limits = cli.call("torrent_limits", hash=torrent["hash"], down=down_bytes, up=up_bytes)
    render.console().print(
        f"[bold]{torrent['name']}[/] — ↓ {fmt_limit(limits['down'])}  ↑ {fmt_limit(limits['up'])}"
    )


@click.command()
@click.argument("down", required=False)
@click.argument("up", required=False)
@handle_errors
def speed(down: str | None, up: str | None) -> None:
    """Global speed limits (human units; 'off' = unlimited).

    \b
    Examples:
      torsh speed            # show
      torsh speed 4M 1M      # cap download and upload
      torsh speed off off    # no limits
    """
    cli = client()
    down_bytes = parse_size(down) if down is not None else None
    up_bytes = parse_size(up) if up is not None else None
    limits = cli.call("global_limits", down=down_bytes, up=up_bytes)
    render.console().print(f"global limits — ↓ {fmt_limit(limits['down'])}  ↑ {fmt_limit(limits['up'])}")


@click.command()
@click.option("--json", "as_json", is_flag=True)
@handle_errors
def stats(as_json: bool) -> None:
    """Session overview: speeds, totals, DHT, listen port."""
    cli = client()
    info_ = cli.call("session")
    if as_json:
        render.emit_json(info_)
        return
    render.console().print(render.session_panel(info_))
