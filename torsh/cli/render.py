"""Rich rendering for the CLI.

One visual vocabulary across every command: the same status glyphs, the same
colours, the same units as the TUI.  Data goes to stdout; progress and
confirmations go to stderr so pipes stay clean (`torsh ls --json | jq` never
sees a spinner).
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from ..units import fmt_eta, fmt_limit, fmt_percent, fmt_rate, fmt_ratio, fmt_size

# Accent palette (mirrors the TUI theme).
CYAN = "#00e8ff"
MAGENTA = "#ff3cac"
VIOLET = "#8f7fff"
GREEN = "#2effa8"
AMBER = "#ffcf6f"
RED = "#ff5f6d"
DIM = "#6b7394"

STATUS_STYLE: dict[str, tuple[str, str]] = {
    "downloading": ("↓", CYAN),
    "seeding": ("↑", GREEN),
    "paused": ("∥", DIM),
    "queued": ("⋯", AMBER),
    "checking": ("⟳", VIOLET),
    "metadata": ("◌", MAGENTA),
    "moving": ("→", AMBER),
    "error": ("✗", RED),
}

_out = Console()
_err = Console(stderr=True)


def console() -> Console:
    return _out


def econsole() -> Console:
    return _err


def ok(message: str) -> None:
    _err.print(f"[bold {GREEN}]✓[/] {message}")


def warn(message: str) -> None:
    _err.print(f"[bold {AMBER}]![/] {message}")


def fail(message: str) -> None:
    _err.print(f"[bold {RED}]✗[/] {message}")


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def status_text(status: str) -> Text:
    glyph, color = STATUS_STYLE.get(status, ("•", "default"))
    return Text(f"{glyph} {status}", style=color)


def progress_cell(fraction: float, status: str, width: int = 14) -> Table | Text:
    """A compact bar + percent, packed into a borderless grid."""
    if status == "metadata" and fraction <= 0:
        # "0.0%" of an unknown size is noise — say what's actually happening.
        return Text("◌ fetching…", style=DIM)
    color = STATUS_STYLE.get(status, ("•", CYAN))[1]
    bar = ProgressBar(
        total=1.0,
        completed=min(1.0, max(0.0, fraction)),
        width=width,
        complete_style=color,
        finished_style=GREEN,
        style="grey23",
    )
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=width)
    grid.add_column(justify="right", width=6)
    grid.add_row(bar, Text(fmt_percent(fraction).strip(), style="bold"))
    return grid


def torrent_table(
    torrents: list[dict[str, Any]], *, title: str | None = None, width: int | None = None
) -> Table:
    """The `torsh ls` table; column set adapts to the terminal width."""
    width = width or _out.width
    wide = width >= 110
    medium = width >= 84

    table = Table(
        title=title,
        header_style=f"bold {DIM}",
        border_style="grey23",
        box=None,
        pad_edge=False,
        expand=False,
        title_style=f"bold {CYAN}",
    )
    name_width = min(48, max(18, width - (92 if wide else 66 if medium else 34)))
    table.add_column("ID", justify="right", style=DIM, no_wrap=True)
    table.add_column("NAME", overflow="ellipsis", max_width=name_width, no_wrap=True)
    table.add_column("PROGRESS", no_wrap=True)
    if medium:
        table.add_column("SIZE", justify="right", no_wrap=True)
        table.add_column("DOWN", justify="right", style=CYAN, no_wrap=True)
        table.add_column("UP", justify="right", style=GREEN, no_wrap=True)
        table.add_column("ETA", justify="right", no_wrap=True)
    if wide:
        table.add_column("RATIO", justify="right", no_wrap=True)
        table.add_column("PEERS", justify="right", style=DIM, no_wrap=True)
    table.add_column("STATUS", no_wrap=True)

    for t in torrents:
        row: list[Any] = [
            str(t["id"]),
            t["name"],
            progress_cell(t["progress"], t["status"], width=14 if medium else 8),
        ]
        if medium:
            row += [
                fmt_size(t["size"]),
                fmt_rate(t["down_rate"]),
                fmt_rate(t["up_rate"]),
                fmt_eta(t["eta"]),
            ]
        if wide:
            row += [fmt_ratio(t["ratio"]), f"{t['seeds']}/{t['peers']}"]
        row.append(status_text(t["status"]))
        table.add_row(*row)
    return table


def watch_view(torrents: list[dict[str, Any]], session: dict[str, Any]) -> Group:
    """One `ls --watch` frame: session totals, the table, an exit hint."""
    limits = session.get("limits", {})
    total = session.get("torrents", 0)
    parts = [
        f"[{GREEN}]●[/] [bold]{total}[/] torrent{'' if total == 1 else 's'}",
        f"[{CYAN}]↓ {fmt_rate(session.get('down_rate'))}[/]",
        f"[{GREEN}]↑ {fmt_rate(session.get('up_rate'))}[/]",
    ]
    if limits.get("down") or limits.get("up"):
        parts.append(f"[{AMBER}]limit ↓{fmt_limit(limits.get('down'))} ↑{fmt_limit(limits.get('up'))}[/]")
    parts.append(f"[{DIM}]dht {session.get('dht_nodes', 0)} · port {session.get('listen_port', 0)}[/]")

    body: Any = torrent_table(torrents) if torrents else Text("nothing to show", style=DIM)
    shown = f"{len(torrents)}/{session.get('torrents', 0)} shown"
    footer = f"[{DIM}]{shown} · Ctrl+C exits, downloads keep running[/]"
    return Group("  ·  ".join(parts), "", body, "", footer)


def kv_panel(title: str, rows: list[tuple[str, Any]], *, border: str = "grey23") -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM, justify="right", no_wrap=True)
    grid.add_column(overflow="fold")
    for key, value in rows:
        grid.add_row(key, value if isinstance(value, Text) else str(value))
    return Panel(
        grid, title=f"[bold {CYAN}]{title}[/]", border_style=border, title_align="left", padding=(1, 2)
    )


def detail_panel(view: dict[str, Any]) -> Panel:
    percent = fmt_percent(view["progress"]).strip()
    done_of_size = f"{percent}  ({fmt_size(view['done'])} of {fmt_size(view['size'])})"
    up_down = f"↓ {fmt_size(view['downloaded'])} / ↑ {fmt_size(view['uploaded'])}"
    transferred = f"{fmt_ratio(view['ratio'])}  ({up_down})"
    rows: list[tuple[str, Any]] = [
        ("status", status_text(view["status"])),
        ("progress", done_of_size),
        ("speed", f"↓ {fmt_rate(view['down_rate'])}   ↑ {fmt_rate(view['up_rate'])}"),
        ("eta", fmt_eta(view["eta"])),
        ("ratio", transferred),
        ("peers", f"{view['peers']} connected, {view['seeds']} seeds"),
        ("path", view["save_path"]),
        ("hash", Text(view["hash"], style=DIM)),
    ]
    if view.get("sequential"):
        rows.insert(2, ("mode", Text("sequential (streaming)", style=MAGENTA)))
    if view.get("queue", -1) >= 0:
        # -1 means "not queued" (paused or manually managed); only a real
        # position is worth a row — it explains a `queued` status.
        rows.append(("queue", f"#{view['queue']}"))
    if view.get("piece_count"):
        pieces = f"{view.get('pieces_have', 0)}/{view['piece_count']} × {fmt_size(view.get('piece_size'))}"
        rows.append(("pieces", pieces))
    if view.get("file_count") is not None:
        rows.append(("files", str(view["file_count"])))
    if view.get("comment"):
        rows.append(("comment", view["comment"]))
    if view.get("private"):
        rows.append(("private", "yes"))
    if view.get("error"):
        rows.append(("error", Text(view["error"], style=RED)))
    return kv_panel(f"#{view['id']}  {view['name']}", rows)


def session_panel(info: dict[str, Any]) -> Panel:
    limits = info.get("limits", {})
    rows = [
        ("torrents", f"{info['torrents']} total — {info['active']} active, {info['paused']} paused"),
        ("speed", f"↓ {fmt_rate(info['down_rate'])}   ↑ {fmt_rate(info['up_rate'])}"),
        ("limits", f"↓ {fmt_limit(limits.get('down'))}   ↑ {fmt_limit(limits.get('up'))}"),
        ("transferred", f"↓ {fmt_size(info['downloaded'])}   ↑ {fmt_size(info['uploaded'])}"),
        ("dht", f"{info['dht_nodes']} nodes"),
        ("listen port", str(info["listen_port"])),
        ("engine", info["engine"]),
    ]
    return kv_panel("session", rows)


def files_table(files: list[dict[str, Any]]) -> Table:
    table = Table(header_style=f"bold {DIM}", box=None, pad_edge=False)
    table.add_column("#", justify="right", style=DIM)
    table.add_column("FILE", overflow="ellipsis", max_width=64)
    table.add_column("SIZE", justify="right")
    table.add_column("DONE", justify="right")
    table.add_column("PRIORITY")
    prio_style = {"skip": DIM, "low": AMBER, "normal": "default", "high": MAGENTA}
    for f in files:
        label = f["priority_label"]
        table.add_row(
            str(f["index"]),
            f["path"],
            fmt_size(f["size"]),
            fmt_percent(f["progress"]).strip(),
            Text(label, style=prio_style.get(label, "default")),
        )
    return table


def peers_table(peers: list[dict[str, Any]]) -> Table:
    table = Table(header_style=f"bold {DIM}", box=None, pad_edge=False)
    table.add_column("ADDRESS")
    table.add_column("CLIENT", overflow="ellipsis", max_width=30)
    table.add_column("DOWN", justify="right", style=CYAN)
    table.add_column("UP", justify="right", style=GREEN)
    table.add_column("HAVE", justify="right")
    table.add_column("VIA", style=DIM)
    for p in peers:
        table.add_row(
            p["address"],
            p["client"],
            fmt_rate(p["down_rate"]),
            fmt_rate(p["up_rate"]),
            fmt_percent(p["progress"]).strip(),
            p["connection"],
        )
    return table


def trackers_table(trackers: list[dict[str, Any]]) -> Table:
    table = Table(header_style=f"bold {DIM}", box=None, pad_edge=False)
    table.add_column("TRACKER", overflow="ellipsis", max_width=56)
    table.add_column("TIER", justify="right", style=DIM)
    table.add_column("SEEDS", justify="right")
    table.add_column("LEECHES", justify="right")
    table.add_column("STATUS")
    for t in trackers:
        if t["working"]:
            status: Text | str = Text("✓ ok", style=GREEN)
        elif t["fails"]:
            status = Text(f"✗ {t['message'] or 'failing'}"[:40], style=RED)
        else:
            status = Text("… waiting", style=DIM)
        table.add_row(
            t["url"],
            str(t["tier"]),
            str(t["seeds"]) if t["seeds"] else "—",
            str(t["leeches"]) if t["leeches"] else "—",
            status,
        )
    return table
