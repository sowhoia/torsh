"""The torsh TUI: a live dashboard over the daemon's RPC socket.

Design notes
------------
* All data arrives over :class:`torsh.ipc.AsyncClient`; the UI owns no
  torrent state beyond the last snapshot, so it can reconnect at any time.
* The table diffs cell-by-cell against row keys (infohashes): no flicker,
  no cursor jumps, updates in O(changed cells).
* Every action goes through the command palette (ctrl+p) as well as a key,
  so features stay discoverable.
"""

from __future__ import annotations

import contextlib
import sys
from functools import partial
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress_bar import ProgressBar
from rich.table import Table as RichTable
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Container, Horizontal
from textual.css.query import NoMatches
from textual.fuzzy import Matcher
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

from .. import __version__, ipc
from .. import config as config_mod
from ..select import GROUPS, SORT_KEYS
from ..units import fmt_eta, fmt_limit, fmt_percent, fmt_rate, fmt_ratio, fmt_size
from .modals import (
    AddTorrentModal,
    HelpModal,
    LimitsModal,
    MoveModal,
    PrioritiesModal,
    RemoveModal,
)

TORSH_THEME = Theme(
    name="torsh",
    primary="#00e8ff",
    secondary="#8f7fff",
    accent="#ff3cac",
    success="#2effa8",
    warning="#ffcf6f",
    error="#ff5f6d",
    foreground="#c9d4ff",
    background="#0a0e1a",
    surface="#10152a",
    panel="#161c33",
    dark=True,
)

STATUS_GLYPHS: dict[str, tuple[str, str]] = {
    "downloading": ("↓", "#00e8ff"),
    "seeding": ("↑", "#2effa8"),
    "paused": ("∥", "#6b7394"),
    "queued": ("⋯", "#ffcf6f"),
    "checking": ("⟳", "#8f7fff"),
    "metadata": ("◌", "#ff3cac"),
    "moving": ("→", "#ffcf6f"),
    "error": ("✗", "#ff5f6d"),
}

STATUS_ORDER = ["all", "downloading", "seeding", "paused", "done", "errored"]

COLUMNS_WIDE = ["sel", "id", "name", "progress", "size", "down", "up", "eta", "ratio", "peers", "status"]
COLUMNS_MEDIUM = ["sel", "id", "name", "progress", "size", "down", "status"]
COLUMNS_NARROW = ["sel", "id", "name", "progress", "status"]
_SORTABLE = ["id", "name", "progress", "size", "down", "up", "eta", "ratio"]

_HEADER_LABELS = {
    "sel": " ",
    "id": "ID",
    "name": "NAME",
    "progress": "PROGRESS",
    "size": "SIZE",
    "down": "DOWN",
    "up": "UP",
    "eta": "ETA",
    "ratio": "RATIO",
    "peers": "PEERS",
    "status": "STATUS",
}

_COLUMN_WIDTHS = {
    "sel": 1,
    "id": 4,
    "progress": 21,
    "size": 9,
    "down": 11,
    "up": 11,
    "eta": 7,
    "ratio": 5,
    "peers": 5,
    "status": 13,
}

_DETAILS_WIDTH = 44
_COMPACT_HEADER_WIDTH = 100


def _expand_uri(uri: str) -> str:
    """Local paths become absolute — the daemon has its own cwd."""
    if uri.startswith(("magnet:", "http://", "https://")):
        return uri
    path = Path(uri).expanduser()
    if path.exists():
        return str(path.resolve())
    return uri


def _absolute_dir(value: str) -> str | None:
    """`~/isos` or `isos` means the user's path, never the daemon's cwd."""
    return str(Path(value).expanduser().resolve()) if value else None


def _pick_columns(table_width: int) -> list[str]:
    if table_width >= 128:
        return COLUMNS_WIDE
    if table_width >= 84:
        return COLUMNS_MEDIUM
    return COLUMNS_NARROW


def status_cell(status: str) -> Text:
    glyph, color = STATUS_GLYPHS.get(status, ("•", "white"))
    return Text(f"{glyph} {status}", style=color)


class TorshCommands(Provider):
    """Command palette provider: every torsh action, fuzzy-searchable."""

    @property
    def _commands(self) -> list[tuple[str, str, Any]]:
        app = self.app
        assert isinstance(app, TorshApp)
        entries: list[tuple[str, str, Any]] = [
            ("add: torrent / magnet / URL", "Open the add dialog", app.action_add),
            ("pause: everything", "Pause all torrents", partial(app.bulk, "pause", "all")),
            ("resume: everything", "Resume all torrents", partial(app.bulk, "resume", "all")),
            ("limits: global speed", "Set global download/upload limits", app.action_global_limits),
            ("queue: move to top", "Run this torrent first", partial(app.action_queue_move, "top")),
            ("queue: move to bottom", "Run this torrent last", partial(app.action_queue_move, "bottom")),
            ("details: toggle panel", "Show or hide the details panel", app.action_toggle_details),
            ("filter: clear", "Clear name filter and status filter", app.clear_filters),
            ("daemon: stop and quit", "Quit and stop the background daemon", app.action_quit_and_stop),
            ("help: keys", "Show the keyboard reference", app.action_help),
        ]
        entries.extend(
            (f"filter: {status}", f"Show {status} torrents", partial(app.set_status_filter, status))
            for status in STATUS_ORDER
        )
        entries.extend(
            (f"sort: {key}", f"Sort the list by {key}", partial(app.set_sort, key)) for key in _SORTABLE
        )
        return entries

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands[:8]:
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher: Matcher = self.matcher(query)
        for name, help_text, callback in self._commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class TorshApp(App):
    """torsh's interactive dashboard."""

    TITLE = "torsh"
    CSS_PATH = "styles.tcss"
    COMMANDS = App.COMMANDS | {TorshCommands}

    BINDINGS = [
        # visible in the footer
        Binding("a", "add", "Add"),
        Binding("space", "pause_resume", "Pause/Go"),
        Binding("d", "remove", "Remove"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("i", "toggle_details", "Info"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        # power keys (documented in help)
        Binding("x", "toggle_select", show=False),
        Binding("asterisk", "select_all", show=False),
        Binding("escape", "clear_ui", show=False, priority=False),
        Binding("s", "sequential", show=False),
        Binding("v", "verify", show=False),
        Binding("t", "reannounce", show=False),
        Binding("m", "move", show=False),
        Binding("l", "limits", show=False),
        Binding("L", "global_limits", show=False),
        Binding("p", "priorities", show=False),
        Binding("shift+up", "queue_move('up')", show=False),
        Binding("shift+down", "queue_move('down')", show=False),
        Binding("f", "cycle_status_filter", show=False),
        Binding("r", "refresh_now", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "cursor_top", show=False),
        Binding("G", "cursor_bottom", show=False),
        *[Binding(str(n), f"sort_column({n})", show=False) for n in range(1, 9)],
    ]

    HELP_SECTIONS = [
        (
            "Act",
            [
                ("a", "add torrents (paste many at once)"),
                ("space", "pause / resume"),
                ("d", "remove (choose: keep or delete files)"),
                ("v / t", "verify data / reannounce"),
                ("m", "move data to another directory"),
            ],
        ),
        (
            "Tune",
            [
                ("s", "toggle sequential (streaming) order"),
                ("p", "per-file priorities"),
                ("l / L", "torrent limits / global limits"),
                ("shift+↑ ↓", "move up / down the download queue"),
            ],
        ),
        (
            "Find",
            [
                ("/", "fuzzy filter by name (Esc clears)"),
                ("f", "cycle status filter"),
                ("1–8", "sort by column (again: reverse)"),
            ],
        ),
        (
            "Select",
            [
                ("x", "select / deselect row"),
                ("*", "select all visible"),
                ("Esc", "clear selection · filter · dialog"),
            ],
        ),
        (
            "Move",
            [
                ("j k / ↑ ↓", "move cursor"),
                ("g / G", "jump to top / bottom"),
                ("i", "toggle the details panel"),
                ("r", "refresh now"),
            ],
        ),
    ]

    def __init__(self, config: config_mod.Config) -> None:
        super().__init__()
        self.config = config
        self.client = ipc.AsyncClient()
        self.torrents: list[dict[str, Any]] = []
        self.view_rows: list[dict[str, Any]] = []
        self.selected: set[str] = set()
        self.filter_text = ""
        self.status_filter = config.ui.status_filter if config.ui.status_filter in STATUS_ORDER else "all"
        sort = config.ui.sort.lstrip("-")
        self.sort_key = sort if sort in SORT_KEYS else "id"
        self.sort_reverse = config.ui.sort.startswith("-")
        self.connected = True
        self.session: dict[str, Any] = {}
        self._down_hist: list[float] = [0.0] * 60
        self._up_hist: list[float] = [0.0] * 60
        self._prev_progress: dict[str, float] = {}
        self._prev_status: dict[str, str] = {}
        self._row_snapshot: dict[str, tuple] = {}
        self._columns = COLUMNS_WIDE
        self._refreshing = False
        self._details_visible = config.ui.show_details
        self._details_timer: Any = None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(f"⌁ torsh [dim]{__version__}[/]", id="brand")
            yield Static("connecting…", id="session-line")
            with Horizontal(classes="spark-box"):
                yield Static("↓", classes="spark-label -down")
                yield Sparkline(self._down_hist, id="spark-down")
            with Horizontal(classes="spark-box"):
                yield Static("↑", classes="spark-label -up")
                yield Sparkline(self._up_hist, id="spark-up")
        with Horizontal(id="filter-bar"):
            yield Input(placeholder="fuzzy filter… (Esc to clear)", id="filter-input")
        with Horizontal(id="main"):
            yield DataTable(id="torrents", cursor_type="row", zebra_stripes=True)
            with Container(id="details"), TabbedContent(initial="overview"):
                with TabPane("Overview", id="overview"):
                    yield Static("", id="overview-body")
                with TabPane("Files", id="files"):
                    yield DataTable(id="files-table", cursor_type="row")
                with TabPane("Peers", id="peers"):
                    yield DataTable(id="peers-table", cursor_type="row")
                with TabPane("Trackers", id="trackers"):
                    yield DataTable(id="trackers-table", cursor_type="row")
        yield Static("", id="status-bar")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(TORSH_THEME)
        self.theme = "torsh"

        self._columns = _pick_columns(self._table_width())
        self._setup_columns()
        self._setup_detail_tables()
        self._reflow_header()
        if self._details_visible:
            self.query_one("#details").add_class("-visible")

        interval = 1.0 / max(0.2, self.config.ui.refresh_hz)
        self.set_interval(interval, self.refresh_data)
        await self.refresh_data()
        self.query_one("#torrents", DataTable).focus()

    async def on_unmount(self) -> None:
        # The daemon deliberately outlives the UI; its socket must not. Textual
        # dispatches Unmount to the App itself on shutdown, so this covers
        # every exit path (q, ctrl+c, the palette, an exception).
        with contextlib.suppress(Exception):
            await self.client.close()

    def _table_width(self) -> int:
        return self.size.width - (_DETAILS_WIDTH if self._details_visible else 0)

    def _setup_columns(self) -> None:
        table = self.query_one("#torrents", DataTable)
        table.clear(columns=True)
        table_width = self._table_width()
        fixed = sum(_COLUMN_WIDTHS[k] for k in self._columns if k != "name")
        padding = 2 * len(self._columns)
        name_width = max(12, min(52, table_width - fixed - padding - 2))
        for key in self._columns:
            width = name_width if key == "name" else _COLUMN_WIDTHS[key]
            table.add_column(_HEADER_LABELS[key], key=key, width=width)
        self._row_snapshot.clear()

    def _setup_detail_tables(self) -> None:
        files = self.query_one("#files-table", DataTable)
        files.add_columns("file", "size", "done", "pri")
        peers = self.query_one("#peers-table", DataTable)
        peers.add_columns("address", "client", "↓", "↑", "have")
        trackers = self.query_one("#trackers-table", DataTable)
        trackers.add_columns("tracker", "seeds", "status")

    def on_resize(self) -> None:
        # `App.is_mounted` is a *method* (is_mounted(widget)), so testing it
        # for truthiness would silently never guard anything — is_running is
        # the App-level "the DOM exists now" flag.
        if not self.is_running:
            return
        self._reflow_header()
        self._reflow_columns()

    def _reflow_header(self) -> None:
        """Below ~100 columns the sparklines crowd out the session summary."""
        with contextlib.suppress(NoMatches):
            self.query_one("#header").set_class(self.size.width < _COMPACT_HEADER_WIDTH, "-compact")

    def _reflow_columns(self) -> None:
        wanted = _pick_columns(self._table_width())
        if wanted is not self._columns:
            self._columns = wanted
            try:
                self._setup_columns()
            except NoMatches:  # a resize can still land before compose() runs
                return
            self._render_table(full=True)

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    async def refresh_data(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            snapshot = await self.client.call("snapshot")
            torrents, session = snapshot["torrents"], snapshot["session"]
            if not self.connected:
                self.connected = True
                self.notify("connection restored", severity="information")
        except Exception:  # noqa: BLE001 - daemon may be restarting
            if self.connected:
                self.connected = False
                self.notify("daemon unreachable — retrying…", severity="warning")
            with contextlib.suppress(NoMatches):
                self._update_session_line()
            return
        finally:
            self._refreshing = False

        self.torrents = torrents
        self.session = session
        try:
            self._notify_transitions(torrents)
            self._append_rates(session)
            self._update_session_line()
            self._apply_view()
            self._render_table()
            if self._details_visible:
                await self._refresh_details()
            self._update_status_bar()
        except NoMatches:
            # A timer tick (or an RPC reply) can land while the DOM is being
            # torn down on exit — a vanished widget just means "stop painting".
            return

    def _notify_transitions(self, torrents: list[dict[str, Any]]) -> None:
        for t in torrents:
            h = t["hash"]
            prev = self._prev_progress.get(h)
            if prev is not None and prev < 1.0 <= t["progress"]:
                self.notify(f"✓ {t['name'][:44]} finished", severity="information", timeout=6)
            prev_status = self._prev_status.get(h)
            if prev_status not in (None, "error") and t["status"] == "error":
                self.notify(f"✗ {t['name'][:38]}: {t['error'][:60]}", severity="error", timeout=8)
            self._prev_progress[h] = t["progress"]
            self._prev_status[h] = t["status"]
        # The loop above re-seeded every live hash, so a size mismatch means
        # (only) that removed torrents are still lingering in the maps.
        if len(self._prev_progress) > len(torrents):
            alive = {t["hash"] for t in torrents}
            self._prev_progress = {h: v for h, v in self._prev_progress.items() if h in alive}
            self._prev_status = {h: v for h, v in self._prev_status.items() if h in alive}

    def _append_rates(self, session: dict[str, Any]) -> None:
        self._down_hist = [*self._down_hist, float(session.get("down_rate", 0))][-60:]
        self._up_hist = [*self._up_hist, float(session.get("up_rate", 0))][-60:]
        self.query_one("#spark-down", Sparkline).data = self._down_hist
        self.query_one("#spark-up", Sparkline).data = self._up_hist

    def _update_session_line(self) -> None:
        line = self.query_one("#session-line", Static)
        if not self.connected:
            line.update("[#ff5f6d]● reconnecting…[/]")
            return
        s = self.session or {}
        limits = s.get("limits", {})
        width = self.size.width
        parts = [
            f"[#2effa8]●[/] {s.get('torrents', 0)} torrents",
            f"[#00e8ff]↓ {fmt_rate(s.get('down_rate'))}[/]",
            f"[#2effa8]↑ {fmt_rate(s.get('up_rate'))}[/]",
        ]
        # Chips drop off right-to-left as the terminal narrows: a session line
        # that wraps would push the header past its fixed three rows.
        if width >= 96 and (limits.get("down") or limits.get("up")):
            parts.append(f"[#ffcf6f]limit ↓{fmt_limit(limits.get('down'))} ↑{fmt_limit(limits.get('up'))}[/]")
        if width >= 76:
            parts.append(f"[dim]dht {s.get('dht_nodes', 0)}[/]")
        line.update(" · ".join(parts))

    # ------------------------------------------------------------------
    # View pipeline: filter → sort → render
    # ------------------------------------------------------------------

    def _apply_view(self) -> None:
        torrents = self.torrents
        if self.status_filter != "all":
            torrents = [t for t in torrents if GROUPS[self.status_filter](t)]
        if self.filter_text:
            matcher = Matcher(self.filter_text)
            torrents = [t for t in torrents if matcher.match(t["name"]) > 0]
        self.view_rows = sorted(torrents, key=SORT_KEYS[self.sort_key], reverse=self.sort_reverse)

    def _row_cells(self, t: dict[str, Any]) -> dict[str, Any]:
        _, color = STATUS_GLYPHS.get(t["status"], ("•", "white"))
        bar = ProgressBar(
            total=1.0,
            completed=t["progress"],
            width=13,
            complete_style=color,
            finished_style="#2effa8",
            style="grey23",
        )
        grid = RichTable.grid(padding=(0, 1))
        grid.add_column(width=13)
        grid.add_column(justify="right", width=6)
        grid.add_row(bar, Text(fmt_percent(t["progress"]).strip()))
        cells = {
            "sel": Text("▌", style="#ff3cac") if t["hash"] in self.selected else Text(" "),
            "id": Text(str(t["id"]), style="#6b7394", justify="right"),
            "name": Text(t["name"], overflow="ellipsis", no_wrap=True),
            "progress": grid,
            "size": Text(fmt_size(t["size"]), justify="right"),
            "down": Text(fmt_rate(t["down_rate"]), style="#00e8ff", justify="right"),
            "up": Text(fmt_rate(t["up_rate"]), style="#2effa8", justify="right"),
            "eta": Text(fmt_eta(t["eta"]), justify="right"),
            "ratio": Text(fmt_ratio(t["ratio"]), justify="right"),
            "peers": Text(f"{t['seeds']}/{t['peers']}", style="#6b7394", justify="right"),
            "status": status_cell(t["status"]),
        }
        return {k: cells[k] for k in self._columns}

    def _row_fingerprint(self, t: dict[str, Any]) -> tuple:
        return (
            t["hash"] in self.selected,
            t["name"],
            round(t["progress"], 3),
            t["size"],
            t["down_rate"],
            t["up_rate"],
            t["eta"],
            round(t["ratio"], 2),
            t["peers"],
            t["seeds"],
            t["status"],
        )

    def _render_table(self, full: bool = False) -> None:
        table = self.query_one("#torrents", DataTable)
        desired = [t["hash"] for t in self.view_rows]
        current = [key.value for key in table.rows]

        if full or desired != current:
            cursor_hash = self._cursor_hash()
            table.clear()
            self._row_snapshot.clear()
            for t in self.view_rows:
                table.add_row(*self._row_cells(t).values(), key=t["hash"])
                self._row_snapshot[t["hash"]] = self._row_fingerprint(t)
            if cursor_hash in desired:
                table.move_cursor(row=desired.index(cursor_hash))
            return

        for t in self.view_rows:
            fingerprint = self._row_fingerprint(t)
            if self._row_snapshot.get(t["hash"]) == fingerprint:
                continue
            cells = self._row_cells(t)
            for column_key, value in cells.items():
                table.update_cell(t["hash"], column_key, value)
            self._row_snapshot[t["hash"]] = fingerprint

    def _cursor_hash(self) -> str | None:
        try:
            table = self.query_one("#torrents", DataTable)
        except NoMatches:  # a timer can land before compose() or after teardown
            return None
        if not table.row_count:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
            return row_key.value
        except Exception:  # noqa: BLE001
            return None

    def _current(self) -> dict[str, Any] | None:
        h = self._cursor_hash()
        return next((t for t in self.view_rows if t["hash"] == h), None)

    def _targets(self) -> list[dict[str, Any]]:
        """Selected torrents, or the cursor row when nothing is selected."""
        if self.selected:
            return [t for t in self.torrents if t["hash"] in self.selected]
        current = self._current()
        return [current] if current else []

    def _update_status_bar(self) -> None:
        parts = []
        if self.selected:
            parts.append(f"[#ff3cac]{len(self.selected)} selected[/]")
        if self.filter_text:
            parts.append(f"filter: [b]{self.filter_text}[/]")
        if self.status_filter != "all":
            parts.append(f"show: [b]{self.status_filter}[/]")
        arrow = "↓" if not self.sort_reverse else "↑"
        parts.append(f"sort: {self.sort_key} {arrow}")
        parts.append(f"{len(self.view_rows)}/{len(self.torrents)}")
        self.query_one("#status-bar", Static).update("  ·  ".join(parts))

    # ------------------------------------------------------------------
    # Details panel
    # ------------------------------------------------------------------

    async def _refresh_details(self) -> None:
        try:
            current = self._current()
            if current is None:
                self.query_one("#overview-body", Static).update("[dim]no torrent under the cursor[/]")
                return
            tabs = self.query_one(TabbedContent)
        except NoMatches:
            # The details timer scheduled on cursor move can land while the
            # DOM is mounting or being torn down — a vanished widget just
            # means "stop painting" (same contract as refresh_data).
            return
        try:
            if tabs.active == "overview":
                detail = await self.client.call("get", hash=current["hash"])
                self.query_one("#overview-body", Static).update(self._overview_renderable(detail))
            elif tabs.active == "files":
                files = await self.client.call("files", hash=current["hash"])
                self._fill_files(files)
            elif tabs.active == "peers":
                peers = await self.client.call("peers", hash=current["hash"])
                self._fill_peers(peers)
            elif tabs.active == "trackers":
                trackers = await self.client.call("trackers", hash=current["hash"])
                self._fill_trackers(trackers)
        except Exception:  # noqa: BLE001 - torrent may vanish mid-refresh
            return

    def _overview_renderable(self, d: dict[str, Any]) -> RichTable:
        grid = RichTable.grid(padding=(0, 1))
        grid.add_column(justify="right", style="#6b7394", no_wrap=True)
        grid.add_column(overflow="fold")
        rows = [
            ("", Text(d["name"], style="bold")),
            ("status", status_cell(d["status"])),
            ("progress", f"{fmt_percent(d['progress']).strip()} of {fmt_size(d['size'])}"),
            ("speed", f"↓ {fmt_rate(d['down_rate'])}  ↑ {fmt_rate(d['up_rate'])}"),
            ("eta", fmt_eta(d["eta"])),
            ("ratio", f"{fmt_ratio(d['ratio'])} (↑ {fmt_size(d['uploaded'])})"),
            ("peers", f"{d['peers']} peers · {d['seeds']} seeds"),
            ("mode", "sequential" if d.get("sequential") else "rarest-first"),
            ("path", d["save_path"]),
        ]
        if d.get("queue", -1) >= 0:
            rows.append(("queue", f"#{d['queue']}"))
        if d.get("piece_count"):
            pieces = f"{d.get('pieces_have', 0)}/{d['piece_count']} × {fmt_size(d.get('piece_size'))}"
            rows.append(("pieces", pieces))
        if d.get("error"):
            rows.append(("error", Text(d["error"], style="#ff5f6d")))
        rows.append(("hash", Text(d["hash"], style="#3d4666")))
        for key, value in rows:
            grid.add_row(key, value if isinstance(value, Text) else str(value))
        return grid

    def _fill_files(self, files: list[dict[str, Any]]) -> None:
        table = self.query_one("#files-table", DataTable)
        table.clear()
        for f in files:
            table.add_row(
                Text(f["path"], overflow="ellipsis", no_wrap=True),
                fmt_size(f["size"]),
                fmt_percent(f["progress"]).strip(),
                f["priority_label"],
                key=str(f["index"]),
            )

    def _fill_peers(self, peers: list[dict[str, Any]]) -> None:
        table = self.query_one("#peers-table", DataTable)
        table.clear()
        for p in peers:
            table.add_row(
                p["address"],
                Text(p["client"], overflow="ellipsis", no_wrap=True),
                fmt_rate(p["down_rate"]),
                fmt_rate(p["up_rate"]),
                fmt_percent(p["progress"]).strip(),
            )

    def _fill_trackers(self, trackers: list[dict[str, Any]]) -> None:
        table = self.query_one("#trackers-table", DataTable)
        table.clear()
        for t in trackers:
            state = "✓" if t["working"] else ("✗" if t["fails"] else "…")
            table.add_row(
                Text(t["url"], overflow="ellipsis", no_wrap=True),
                str(t["seeds"]) if t["seeds"] else "—",
                f"{state} {t['message'][:24]}" if t["message"] else state,
            )

    # ------------------------------------------------------------------
    # RPC helpers
    # ------------------------------------------------------------------

    @work(exclusive=False)
    async def bulk(self, method: str, scope: str | None = None) -> None:
        """Run a bulk RPC (pause/resume/recheck/reannounce) and report."""
        source = self.torrents if scope == "all" else self._targets()
        hashes = [t["hash"] for t in source]
        if not hashes:
            return
        try:
            count = await self.client.call(method, hashes=hashes)
            self.notify(f"{method}: {count} torrent(s)", severity="information", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
        await self.refresh_data()

    # ------------------------------------------------------------------
    # Actions — navigation & selection
    # ------------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#torrents", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#torrents", DataTable).action_cursor_up()

    def action_cursor_top(self) -> None:
        self.query_one("#torrents", DataTable).move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        table = self.query_one("#torrents", DataTable)
        table.move_cursor(row=max(0, table.row_count - 1))

    def action_toggle_select(self) -> None:
        current = self._current()
        if current is None:
            return
        h = current["hash"]
        if h in self.selected:
            self.selected.discard(h)
        else:
            self.selected.add(h)
        self._row_snapshot.pop(h, None)  # force this row to re-render
        self._render_table()
        self.action_cursor_down()
        self._update_status_bar()

    def action_select_all(self) -> None:
        if len(self.selected) == len(self.view_rows):
            self.selected.clear()
        else:
            self.selected = {t["hash"] for t in self.view_rows}
        self._row_snapshot.clear()
        self._render_table()
        self._update_status_bar()

    def action_clear_ui(self) -> None:
        filter_input = self.query_one("#filter-input", Input)
        if filter_input.has_focus:
            filter_input.value = ""
            self.filter_text = ""
            self.query_one("#filter-bar").remove_class("-visible")
            self.query_one("#torrents", DataTable).focus()
        elif self.filter_text or self.status_filter != "all":
            self.clear_filters()
        elif self.selected:
            self.selected.clear()
            self._row_snapshot.clear()
            self._render_table()
        self._apply_view()
        self._render_table()
        self._update_status_bar()

    # ------------------------------------------------------------------
    # Actions — filter & sort
    # ------------------------------------------------------------------

    def action_focus_filter(self) -> None:
        self.query_one("#filter-bar").add_class("-visible")
        self.query_one("#filter-input", Input).focus()

    @on(Input.Changed, "#filter-input")
    def _on_filter_change(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self._apply_view()
        self._render_table(full=True)
        self._update_status_bar()

    @on(Input.Submitted, "#filter-input")
    def _on_filter_submit(self) -> None:
        self.query_one("#torrents", DataTable).focus()
        if not self.filter_text:
            self.query_one("#filter-bar").remove_class("-visible")

    def clear_filters(self) -> None:
        self.filter_text = ""
        self.status_filter = "all"
        self.query_one("#filter-input", Input).value = ""
        self.query_one("#filter-bar").remove_class("-visible")
        self._apply_view()
        self._render_table(full=True)
        self._update_status_bar()

    def set_status_filter(self, status: str) -> None:
        self.status_filter = status
        self._apply_view()
        self._render_table(full=True)
        self._update_status_bar()
        self._persist_ui()

    def action_cycle_status_filter(self) -> None:
        index = STATUS_ORDER.index(self.status_filter) if self.status_filter in STATUS_ORDER else 0
        self.set_status_filter(STATUS_ORDER[(index + 1) % len(STATUS_ORDER)])
        self.notify(f"showing: {self.status_filter}", timeout=2)

    def set_sort(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key, self.sort_reverse = key, False
        self._apply_view()
        self._render_table(full=True)
        self._update_status_bar()
        self._persist_ui()

    def action_sort_column(self, number: int) -> None:
        if 1 <= number <= len(_SORTABLE):
            self.set_sort(_SORTABLE[number - 1])

    @on(DataTable.HeaderSelected, "#torrents")
    def _on_header(self, event: DataTable.HeaderSelected) -> None:
        key = event.column_key.value
        if key in SORT_KEYS:
            self.set_sort(key)

    @on(DataTable.RowHighlighted, "#torrents")
    def _on_row_highlight(self) -> None:
        # Debounced: holding j/k must not fire one RPC per row crossed.
        if not self._details_visible:
            return
        if self._details_timer is not None:
            self._details_timer.stop()
        self._details_timer = self.set_timer(0.15, self._refresh_details)

    @on(TabbedContent.TabActivated)
    async def _on_tab(self) -> None:
        if self._details_visible:
            await self._refresh_details()

    # ------------------------------------------------------------------
    # Actions — torrent operations
    # ------------------------------------------------------------------

    @work
    async def action_add(self) -> None:
        result = await self.push_screen_wait(
            AddTorrentModal(str(self.config.downloads.resolved_dir()), self.config.downloads.sequential)
        )
        if not result:
            return
        directory = _absolute_dir(result["dir"])
        added = failed = 0
        for uri in result["uris"]:
            try:
                view = await self.client.call(
                    "add",
                    uri=_expand_uri(uri),
                    dir=directory,
                    paused=result["paused"],
                    sequential=result["sequential"],
                )
                added += 1
                self.notify(f"+ {view['name'][:44]}", severity="information", timeout=4)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.notify(f"✗ {str(exc)[:70]}", severity="error", timeout=6)
        if added:
            await self.refresh_data()

    def action_pause_resume(self) -> None:
        targets = self._targets()
        if not targets:
            return
        active = [t for t in targets if t["status"] not in {"paused", "queued"}]
        self.bulk("pause" if active else "resume")

    @work
    async def action_remove(self) -> None:
        targets = self._targets()
        if not targets:
            return
        if self.config.ui.confirm_delete:
            mode = await self.push_screen_wait(RemoveModal([t["name"] for t in targets]))
            if mode is None:
                return
        else:
            mode = "keep"  # no-confirm mode never deletes data implicitly
        try:
            count = await self.client.call(
                "remove", hashes=[t["hash"] for t in targets], delete_data=mode == "data"
            )
            for t in targets:
                self.selected.discard(t["hash"])
            self.notify(
                f"removed {count} torrent(s)" + (" + files" if mode == "data" else ""),
                severity="warning",
                timeout=4,
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
        await self.refresh_data()

    def action_verify(self) -> None:
        self.bulk("recheck")

    def action_reannounce(self) -> None:
        self.bulk("reannounce")

    @work
    async def action_sequential(self) -> None:
        targets = self._targets()
        if not targets:
            return
        enable = not targets[0]["sequential"]
        for t in targets:
            try:
                await self.client.call("sequential", hash=t["hash"], enabled=enable)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), severity="error")
                return
        self.notify(f"sequential {'on' if enable else 'off'} · {len(targets)} torrent(s)", timeout=3)
        await self.refresh_data()

    @work
    async def action_queue_move(self, move: str) -> None:
        current = self._current()
        if current is None:
            return
        try:
            position = await self.client.call("queue", hash=current["hash"], move=move)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        self.notify(f"queue #{position} · {current['name'][:36]}", timeout=2)
        await self.refresh_data()

    @work
    async def action_move(self) -> None:
        current = self._current()
        if current is None:
            return
        new_dir = await self.push_screen_wait(MoveModal(current["name"], current["save_path"]))
        if not new_dir:
            return
        try:
            await self.client.call("move", hash=current["hash"], dir=new_dir)
            self.notify(f"moving → {new_dir}", timeout=4)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")

    @work
    async def action_limits(self) -> None:
        # Every RPC is wrapped: an unhandled worker exception exits the whole
        # app (Textual's exit_on_error), and the daemon may vanish at any time.
        current = self._current()
        if current is None:
            return
        try:
            limits = await self.client.call("torrent_limits", hash=current["hash"])
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        result = await self.push_screen_wait(
            LimitsModal(f"Limits — {current['name'][:40]}", limits["down"], limits["up"])
        )
        if result is None:
            return
        down, up = result
        try:
            await self.client.call("torrent_limits", hash=current["hash"], down=down, up=up)
            self.notify(f"limits: ↓ {fmt_limit(down)} ↑ {fmt_limit(up)}", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")

    @work
    async def action_global_limits(self) -> None:
        try:
            limits = await self.client.call("global_limits")
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        result = await self.push_screen_wait(LimitsModal("Global limits", limits["down"], limits["up"]))
        if result is None:
            return
        down, up = result
        try:
            await self.client.call("global_limits", down=down, up=up)
            self.notify(f"global limits: ↓ {fmt_limit(down)} ↑ {fmt_limit(up)}", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
        await self.refresh_data()

    @work
    async def action_priorities(self) -> None:
        current = self._current()
        if current is None:
            return
        try:
            files = await self.client.call("files", hash=current["hash"])
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")
            return
        if not files:
            self.notify("metadata not downloaded yet", severity="warning")
            return
        changes = await self.push_screen_wait(PrioritiesModal(current["name"], files))
        if not changes:
            return
        try:
            await self.client.call("priorities", hash=current["hash"], changes=changes)
            self.notify(f"priorities updated ({len(changes)} file(s))", timeout=3)
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error")

    def action_toggle_details(self) -> None:
        self._details_visible = not self._details_visible
        panel = self.query_one("#details")
        panel.set_class(self._details_visible, "-visible")
        if self._details_visible:
            self.call_after_refresh(self._refresh_details)
        self._reflow_columns()  # the table just gained/lost ~44 columns of width
        self._persist_ui()

    async def action_refresh_now(self) -> None:
        await self.refresh_data()

    @work
    async def action_help(self) -> None:
        await self.push_screen_wait(HelpModal(self.HELP_SECTIONS))

    @work
    async def action_quit_and_stop(self) -> None:
        with contextlib.suppress(Exception):  # it may already be gone
            await self.client.call("shutdown")
        self.exit(message="daemon stopped — nothing is seeding now")

    async def action_quit(self) -> None:
        # async to match App.action_quit's signature — Textual invokes actions
        # through `invoke()`, but a callback may also await it directly.
        active = sum(1 for t in self.torrents if t["status"] in {"downloading", "seeding", "metadata"})
        message = None
        if active:
            message = (
                f"{active} torrent(s) keep running in the daemon — "
                "`torsh ls` to peek, `torsh daemon stop` to stop everything"
            )
        self.exit(message=message)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_ui(self) -> None:
        # Reload-modify-save (env=False): saving the startup snapshot back
        # would bake TORSH_* overrides into the file and revert any setting
        # changed elsewhere (`torsh config set`, `torsh speed`) while the
        # TUI was open. Only the [ui] section belongs to us.
        self.config.ui.sort = ("-" if self.sort_reverse else "") + self.sort_key
        self.config.ui.status_filter = self.status_filter
        self.config.ui.show_details = self._details_visible
        with contextlib.suppress(OSError):
            fresh = config_mod.load(env=False)
            fresh.ui = self.config.ui
            config_mod.save(fresh)


def run_tui() -> None:
    """CLI entry: make sure the daemon is up, then hand over the terminal."""
    console = Console(stderr=True)
    config = config_mod.load()

    if not ipc.daemon_running():
        if not config.daemon.autostart:
            console.print("[#ff5f6d]✗ daemon is not running[/] — start it with [bold]torsh daemon start[/]")
            sys.exit(1)
        with console.status("[#00e8ff]starting the torsh daemon…", spinner="dots"):
            try:
                ipc.ensure_daemon(autostart=True).close()
            except ipc.DaemonUnavailable as exc:
                console.print(f"[#ff5f6d]✗ {exc}[/]")
                sys.exit(1)

    app = TorshApp(config)
    app.run()  # textual prints the app's exit message (background hint) itself
    console.print("[#6b7394]torsh out — happy seeding.[/]")
