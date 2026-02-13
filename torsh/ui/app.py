"""
Torsh TUI Application - Enhanced Version

A polished, feature-rich Textual-based torrent client UI.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from rich.progress_bar import ProgressBar
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    Markdown,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from transmission_rpc.error import TransmissionError

import humanize

from ..client import TorrentView, TransmissionController
from ..config import AppConfig, save_config
from ..daemon import maybe_start_daemon
from ..logging import get_logger
from .modals import (
    AddTorrentScreen,
    ConfirmScreen,
    FilterScreen,
    HelpScreen,
    MoveScreen,
    PriorityScreen,
    SpeedScreen,
)

LOG = get_logger(__name__)



# =============================================================================
# Helper Functions
# =============================================================================

def format_percent(value: float) -> str:
    """Format a percentage value with fixed width."""
    return f"{value:5.1f}%"


def styled_status(status: str) -> Text:
    """Return a styled Text object for torrent status."""
    styles = {
        "downloading": ("⬇", "bold green"),
        "seeding": ("⬆", "bold blue"),
        "stopped": ("⏸", "dim"),
        "paused": ("⏸", "dim yellow"),
        "checking": ("⟳", "magenta"),
        "queued": ("⏳", "cyan"),
        "error": ("⚠", "bold red"),
    }
    icon, style = styles.get(status.lower(), ("?", "default"))
    return Text(f"{icon} {status.title()}", style=style)


def styled_ratio(ratio: float) -> Text:
    """Return a styled Text object for ratio."""
    style = "bold green" if ratio >= 1.0 else "bold red"
    return Text(f"{ratio:.2f}", style=style, justify="right")


# =============================================================================
# Modal Screens live in torsh.ui.modals
# =============================================================================


# =============================================================================
# Main Application
# =============================================================================

class TorshApp(App):
    """Main Torsh TUI Application."""

    CSS_PATH = "styles.tcss"
    
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("space", "toggle", "Pause/Start"),
        Binding("r", "refresh", "Refresh"),
        Binding("?", "help", "Help"),
        Binding("g", "move", "Move"),
        Binding("s", "speed", "Speed"),
        Binding("t", "torrent_speed", "T-Speed"),
        Binding("v", "verify", "Verify"),
        Binding("p", "priority", "Priority"),
        Binding("/", "filter", "Filter"),
        Binding("c", "status_filter", "Status"),
        Binding("o", "progress_filter", "Progress"),
        Binding("x", "delete_keep", "Del(K)"),
        Binding("]", "faster", show=False),
        Binding("[", "slower", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "cursor_bottom", show=False),
        Binding("1", "sort_1", show=False),
        Binding("2", "sort_2", show=False),
        Binding("3", "sort_3", show=False),
        Binding("4", "sort_4", show=False),
        Binding("5", "sort_5", show=False),
        Binding("6", "sort_6", show=False),
        Binding("7", "sort_7", show=False),
        Binding("8", "sort_8", show=False),
    ]

    # Reactive state
    download_speed = reactive(0.0)
    upload_speed = reactive(0.0)
    active_count = reactive(0)
    paused_count = reactive(0)
    connection_ok = reactive(True)
    refresh_interval = reactive(2.5)
    sort_column: reactive[int | None] = reactive(None)
    sort_desc = reactive(False)
    status_filter_value = reactive("any")
    progress_filter_value = reactive("any")
    disk_free = reactive(0)
    disk_total = reactive(1)

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.controller = TransmissionController(config)
        
        self.torrents: list[TorrentView] = []
        self.filtered: list[TorrentView] = []
        self.selected_id: int | None = None
        self.filter_text: str = config.ui.filter_text
        
        self._refresh_timer: Any = None
        self._modal_depth: int = 0
        self._speed_down_hist: list[float] = [0.0] * 60
        self._speed_up_hist: list[float] = [0.0] * 60
        self._completed_ids: set[int] = set()  # Track completed torrents
        self._row_cache: dict[int, dict[str, Any]] = {}
        self._files_cache: dict[int, dict[str, Any]] = {}
        self._trackers_cache: dict[str, dict[str, Any]] = {}
        self._files_torrent_id: int | None = None
        self._trackers_torrent_id: int | None = None
        self._table_columns: dict[str, Any] = {}
        self._files_columns: dict[str, Any] = {}
        self._trackers_columns: dict[str, Any] = {}
        self.global_speed_limit_down: int = 0
        self.global_speed_limit_up: int = 0
        self._connection_state: bool = True
        self._last_refresh_error: bool = False
        self._auto_retry_attempts: dict[int, int] = {}
        self._verified_ids: set[int] = set()
        self._user_paused: set[int] = set()
        self._auto_started: set[int] = set()
        
        # Restore state
        self.refresh_interval = config.ui.refresh_interval
        self.sort_column = config.ui.sort_column
        self.sort_desc = config.ui.sort_desc
        self.status_filter_value = config.ui.status_filter
        self.progress_filter_value = config.ui.progress_filter

    # -------------------------------------------------------------------------
    # Compose & Mount
    # -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Header with status bar
        with Container(id="app-header"):
            with Container(id="title-stack"):
                yield Label("TORSH", classes="app-title", id="title-art")
                yield Static("", id="status-bar")
            with Horizontal(id="header-stats"):
                with Horizontal(classes="stat-box"):
                    yield Label("Disk ", classes="stat-label")
                    yield Static("", id="disk-bar", markup=False)
                with Horizontal(classes="stat-box"):
                    yield Label("Limit ", classes="stat-label")
                    yield Static("∞ / ∞", id="limit-badge")
                with Horizontal(classes="stat-box"):
                    yield Label("↓ ", classes="stat-label")
                    yield Sparkline(self._speed_down_hist, summary_function=max, classes="-download")
                with Horizontal(classes="stat-box"):
                    yield Label("↑ ", classes="stat-label")
                    yield Sparkline(self._speed_up_hist, summary_function=max, classes="-upload")

        # Main content
        with Container(id="content"):
            with Container(id="torrent-list-container"):
                yield DataTable(id="table", zebra_stripes=True, cursor_type="row")
            with Container(id="details-container"):
                with TabbedContent(initial="info"):
                    with TabPane("Info", id="info"):
                        yield Markdown("", id="details-view")
                    with TabPane("Files", id="files"):
                        yield DataTable(id="files-table", cursor_type="row")
                    with TabPane("Trackers", id="trackers"):
                        yield DataTable(id="trackers-table", cursor_type="row")
        yield Static("", id="bindings-bar", markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        # Setup main table
        table = self.query_one("#table", DataTable)
        cols = table.add_columns(
            ("ID", "id"),
            ("Name", "name"),
            ("Progress", "progress"),
            ("ETA", "eta"),
            ("↓", "down"),
            ("↑", "up"),
            ("Ratio", "ratio"),
            ("Status", "status"),
        )
        self._table_columns = {
            "id": cols[0],
            "name": cols[1],
            "progress": cols[2],
            "eta": cols[3],
            "down": cols[4],
            "up": cols[5],
            "ratio": cols[6],
            "status": cols[7],
        }
        
        # Setup files table
        files_table = self.query_one("#files-table", DataTable)
        file_cols = files_table.add_columns(
            ("Name", "name"),
            ("Size", "size"),
            ("Done", "done"),
            ("Pri", "priority"),
        )
        self._files_columns = {
            "name": file_cols[0],
            "size": file_cols[1],
            "done": file_cols[2],
            "priority": file_cols[3],
        }
        
        # Setup trackers table
        trackers_table = self.query_one("#trackers-table", DataTable)
        tracker_cols = trackers_table.add_columns(
            ("Host", "host"),
            ("Status", "status"),
            ("Peers", "peers"),
            ("S", "seeders"),
            ("L", "leechers"),
        )
        self._trackers_columns = {
            "host": tracker_cols[0],
            "status": tracker_cols[1],
            "peers": tracker_cols[2],
            "seeders": tracker_cols[3],
            "leechers": tracker_cols[4],
        }

        # Initialize sparklines
        self.query_one("Sparkline.-download", Sparkline).data = self._speed_down_hist
        self.query_one("Sparkline.-upload", Sparkline).data = self._speed_up_hist
        
        self._set_refresh_interval(self.refresh_interval)
        await self.refresh_all()
        self._update_bindings_bar()

    # -------------------------------------------------------------------------
    # Status Bar
    # -------------------------------------------------------------------------

    def _update_status_bar(self) -> None:
        """Update the status bar with current state info."""
        parts = []
        
        # Connection status
        if self.connection_ok:
            parts.append("[green]●[/] Connected")
        else:
            parts.append("[red]○[/] Disconnected")
        
        # Torrent counts
        parts.append(f"[cyan]{self.active_count}[/]↓ [dim]{self.paused_count}[/]⏸")
        
        # Filter indicator
        if self.filter_text:
            parts.append(f"[yellow]Filter:[/] {self.filter_text[:10]}")
        if self.status_filter_value != "any":
            parts.append(f"[magenta]{self.status_filter_value}[/]")
        
        # Speed limits indicator
        limit_down = self._format_limit(self.global_speed_limit_down)
        limit_up = self._format_limit(self.global_speed_limit_up)
        parts.append(f"Limit ↓{limit_down}/↑{limit_up}")
        
        # Refresh rate
        parts.append(f"[dim]{self.refresh_interval:.1f}s[/]")
        
        status_bar = self.query_one("#status-bar", Static)
        status_bar.update(" │ ".join(parts))
        self._update_bindings_bar()

    def _update_bindings_bar(self) -> None:
        """Render one-line shortcut hints."""
        hint = (
            "[bold cyan]a[/] Add  │ "
            "[bold cyan]d[/] Del  │ "
            "[bold cyan]x[/] Del keep  │ "
            "[bold cyan]space[/] Pause/Start  │ "
            "[bold cyan]v[/] Verify  │ "
            "[bold cyan]g[/] Move  │ "
            "[bold cyan]s[/] Speed  │ "
            "[bold cyan]t[/] T-speed  │ "
            "[bold cyan]p[/] Priority  │ "
            "[bold cyan]/[/] Filter  │ "
            "[bold cyan]c[/] Status  │ "
            "[bold cyan]o[/] Progress  │ "
            "[bold cyan]?[/] Help  │ "
            "[bold cyan]q[/] Quit"
        )
        try:
            self.query_one("#bindings-bar", Static).update(hint)
        except Exception as exc:
            LOG.debug(f"Failed to update bindings bar: {exc}")

    def _update_limit_badge(self) -> None:
        """Update the header speed limit badge."""
        try:
            badge = self.query_one("#limit-badge", Static)
        except Exception as exc:
            LOG.debug(f"Failed to query limit badge: {exc}")
            return
        limit_down = self._format_limit(self.global_speed_limit_down)
        limit_up = self._format_limit(self.global_speed_limit_up)
        badge.update(f"↓ {limit_down} / ↑ {limit_up}")

    @staticmethod
    def _format_limit(value: int | None) -> str:
        return "∞" if not value else str(value)

    # -------------------------------------------------------------------------
    # Data Refresh
    # -------------------------------------------------------------------------

    def _set_refresh_interval(self, value: float) -> None:
        if self._refresh_timer:
            self._refresh_timer()
        value = max(0.8, min(10.0, value))
        self.refresh_interval = value
        self._refresh_timer = self.set_interval(value, self.refresh_all)
        self._persist_ui()

    async def refresh_all(self) -> None:
        # Пропускаем автообновление, пока открыт модальный экран,
        # чтобы не дёргать недоступные виджеты и не сбивать ввод.
        if self._modal_depth:
            return
        if not await self._check_connection():
            return
        await asyncio.gather(
            self._refresh_torrents(),
            self._refresh_stats(),
        )
        self._update_status_bar()

    async def _check_connection(self) -> bool:
        previous_state = self._connection_state
        connected = False
        try:
            await self.controller.ensure_connected()
            self.connection_ok = True
            connected = True
        except TransmissionError:
            self.connection_ok = False
        except Exception:
            self.connection_ok = False
            if self.config.daemon.restart_on_fail and self.config.daemon.autostart:
                maybe_start_daemon(self.config)
                await asyncio.sleep(1.5)
                try:
                    await self.controller.ensure_connected()
                    self.connection_ok = True
                    self.notify("🔄 Daemon restarted", severity="warning")
                    connected = True
                except Exception as restart_exc:
                    LOG.debug(f"Daemon restart failed: {restart_exc}")
        if self.connection_ok != previous_state:
            self._notify_connection_change(self.connection_ok)
        self._connection_state = self.connection_ok
        if not self.connection_ok:
            self._update_status_bar()
        return connected

    def _notify_connection_change(self, connected: bool) -> None:
        """Show a one-shot notification on connection changes."""
        message = "🟢 Connection restored" if connected else "⚠️ Connection lost"
        severity = "information" if connected else "warning"
        self.notify(message, severity=severity)

    async def _auto_verify(self, torrent_id: int, name: str) -> None:
        """Trigger a one-time verify after completion."""
        if torrent_id in self._verified_ids:
            return
        try:
            await self.controller.verify([torrent_id])
            self._verified_ids.add(torrent_id)
            self.notify(f"✅ Verified: {name[:30]}", severity="information")
        except Exception as exc:
            LOG.debug(f"Auto-verify failed for {torrent_id}: {exc}")

    async def _auto_retry_failed(self, torrents: list[TorrentView]) -> None:
        """Auto-retry torrents that are in error state."""
        for t in torrents:
            status = t.status.lower()
            if "error" in status and t.percent_done < 100.0:
                attempts = self._auto_retry_attempts.get(t.id, 0)
                if attempts >= 3:
                    continue
                self._auto_retry_attempts[t.id] = attempts + 1
                try:
                    await self.controller.start([t.id])
                    self.notify(f"🔁 Повторная попытка #{attempts + 1}: {t.name[:30]}", severity="warning")
                except Exception as exc:
                    LOG.debug(f"Auto-retry failed for {t.id}: {exc}")
            else:
                if t.id in self._auto_retry_attempts:
                    self._auto_retry_attempts.pop(t.id, None)

    async def _auto_resume(self, torrents: list[TorrentView]) -> None:
        """Auto-start paused/stopped torrents unless user paused them."""
        to_start: list[int] = []
        for t in torrents:
            status = t.status.lower()
            if t.percent_done >= 100.0:
                continue
            if status in {"stopped", "paused"} and t.id not in self._user_paused:
                to_start.append(t.id)
        if not to_start:
            return
        try:
            await self.controller.start(to_start)
            for tid in to_start:
                self._auto_started.add(tid)
            self.notify(f"▶ Автостарт {len(to_start)} торрентов", severity="information")
        except Exception as exc:
            LOG.debug(f"Auto-resume failed: {exc}")

    async def _refresh_torrents(self) -> None:
        try:
            old_torrents = {t.id: t for t in self.torrents}
            self.torrents = await self.controller.list_torrents()
            
            # Check for newly completed downloads
            for t in self.torrents:
                if t.percent_done >= 100.0 and t.id not in self._completed_ids:
                    if t.id in old_torrents and old_torrents[t.id].percent_done < 100.0:
                        self.notify(f"✅ Completed: {t.name[:30]}", severity="information")
                    self._completed_ids.add(t.id)
                    await self._auto_verify(t.id, t.name)
            
            self._apply_filter()
            await self._auto_retry_failed(self.torrents)
            await self._auto_resume(self.torrents)
            self._render_table()
            self._last_refresh_error = False
        except Exception as exc:
            if not self._last_refresh_error:
                self.notify(f"⚠️ Refresh error: {exc}", severity="error")
            self._last_refresh_error = True
            LOG.error(f"Refresh error: {exc}")

    async def _refresh_stats(self) -> None:
        try:
            stats = await self.controller.session_stats()
            self.download_speed = (getattr(stats, "download_speed", 0) or 0) / 1024
            self.upload_speed = (getattr(stats, "upload_speed", 0) or 0) / 1024
            self._append_speed(self.download_speed, self.upload_speed)
            self.active_count = getattr(stats, "active_torrent_count", 0) or 0
            self.paused_count = getattr(stats, "paused_torrent_count", 0) or 0
            self._update_disk()
            self._render_disk_bar()
            try:
                limits = await self.controller.get_speed_limits()
                self.global_speed_limit_down = limits.get("down", 0)
                self.global_speed_limit_up = limits.get("up", 0)
                self._update_limit_badge()
            except Exception as limits_error:
                LOG.debug(f"Speed limits fetch failed: {limits_error}")
        except Exception as exc:
            LOG.error(f"Stats error: {exc}")

    # -------------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------------

    def _render_table(self) -> None:
        table = self.query_one("#table", DataTable)
        data = self._sorted(self.filtered or self.torrents)
        desired_keys = [str(t.id) for t in data]

        # Remove rows that disappeared
        for row in list(table.ordered_rows):
            if row.key.value not in desired_keys:
                table.remove_row(row.key)
                self._row_cache.pop(int(row.key.value), None)

        row_key_map = {row.key.value: row.key for row in table.ordered_rows}
        row_obj_map = {row.key.value: row for row in table.ordered_rows}

        for torrent in data:
            cells, snapshot = self._torrent_cells(torrent)
            key_str = str(torrent.id)
            row_key = row_key_map.get(key_str)
            if row_key is None:
                row_key = table.add_row(*cells, key=key_str)
                row_key_map[key_str] = row_key
            else:
                cached = self._row_cache.get(torrent.id)
                if cached != snapshot:
                    self._update_torrent_row(table, row_key, cells, cached, snapshot)
            self._row_cache[torrent.id] = snapshot

        self._sync_table_order(table, desired_keys, row_obj_map)

        if self.selected_id is not None:
            idx = self._find_row_index(self.selected_id, data)
            if idx is not None:
                table.cursor_coordinate = (idx, 0)
            else:
                self.selected_id = None
        
        if self.selected_id is None and data:
            self.selected_id = data[0].id
            table.cursor_coordinate = (0, 0)
        
        self._render_details()

    def _torrent_cells(self, torrent: TorrentView) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Build renderable cells and a lightweight snapshot for diff updates."""
        progress_bar = ProgressBar(
            total=100.0,
            completed=torrent.percent_done,
            width=12,
            pulse=torrent.percent_done < 100.0 and torrent.status == "downloading",
            complete_style="bold cyan",
            finished_style="magenta",
        )
        down_style = "bold green" if torrent.rate_down != "0 B/s" else "dim"
        up_style = "bold blue" if torrent.rate_up != "0 B/s" else "dim"

        cells = (
            Text(str(torrent.id), justify="right"),
            Text(torrent.name, overflow="ellipsis", no_wrap=True),
            progress_bar,
            Text(torrent.eta, justify="right"),
            Text(torrent.rate_down, style=down_style, justify="right"),
            Text(torrent.rate_up, style=up_style, justify="right"),
            styled_ratio(torrent.ratio),
            styled_status(torrent.status),
        )
        snapshot = {
            "name": torrent.name,
            "progress": round(torrent.percent_done, 2),
            "eta": torrent.eta,
            "rate_down": torrent.rate_down,
            "rate_up": torrent.rate_up,
            "ratio": round(torrent.ratio, 3),
            "status": torrent.status,
        }
        return cells, snapshot

    def _update_torrent_row(
        self,
        table: DataTable,
        row_key: Any,
        cells: tuple[Any, ...],
        cached: dict[str, Any] | None,
        snapshot: dict[str, Any],
    ) -> None:
        """Update only the cells that actually changed."""

        def changed(field: str) -> bool:
            return cached is None or cached.get(field) != snapshot[field]

        if changed("name"):
            table.update_cell(row_key, self._table_columns["name"], cells[1])
        if changed("progress") or changed("eta"):
            table.update_cell(row_key, self._table_columns["progress"], cells[2])
            table.update_cell(row_key, self._table_columns["eta"], cells[3])
        if changed("rate_down"):
            table.update_cell(row_key, self._table_columns["down"], cells[4])
        if changed("rate_up"):
            table.update_cell(row_key, self._table_columns["up"], cells[5])
        if changed("ratio"):
            table.update_cell(row_key, self._table_columns["ratio"], cells[6])
        if changed("status"):
            table.update_cell(row_key, self._table_columns["status"], cells[7])

    def _sync_table_order(
        self,
        table: DataTable,
        desired_keys: list[str],
        row_obj_map: dict[str, Any],
    ) -> None:
        """Reorder rows to match sorted data without nuking the table."""
        try:
            ordered_rows = []
            for key in desired_keys:
                row_obj = row_obj_map.get(key)
                if row_obj is not None:
                    ordered_rows.append(row_obj)
            if ordered_rows and ordered_rows != table.ordered_rows:
                table.ordered_rows[:] = ordered_rows
                table.refresh(layout=False)
        except Exception as exc:
            LOG.debug(f"Row reorder skipped: {exc}")

    def _find_row_index(self, torrent_id: int, data: list[TorrentView]) -> int | None:
        for idx, t in enumerate(data):
            if t.id == torrent_id:
                return idx
        return None

    def _render_details(self) -> None:
        details = self.query_one("#details-view", Markdown)
        data = self._sorted(self.filtered or self.torrents)
        torrent = next((t for t in data if t.id == self.selected_id), None)
        
        if torrent:
            if self._files_torrent_id != torrent.id:
                self._files_torrent_id = torrent.id
                self._files_cache.clear()
                self.query_one("#files-table", DataTable).clear()
            if self._trackers_torrent_id != torrent.id:
                self._trackers_torrent_id = torrent.id
                self._trackers_cache.clear()
                self.query_one("#trackers-table", DataTable).clear()
            md = f"""
## {torrent.name}

| Property | Value |
|----------|-------|
| Status | {styled_status(torrent.status).plain} |
| Progress | {torrent.percent_done:.1f}% |
| Size | {torrent.size} |
| Ratio | {torrent.ratio:.2f} |
| ETA | {torrent.eta} |
| Peers | {torrent.peers} (S:{torrent.seeders}/L:{torrent.leechers}) |
| Path | `{torrent.download_dir}` |
"""
            details.update(md)
            asyncio.create_task(self._update_files_tab(torrent.id))
            asyncio.create_task(self._update_trackers_tab(torrent.id))
        else:
            details.update("_Select a torrent to view details_")
            self.query_one("#files-table", DataTable).clear()
            self.query_one("#trackers-table", DataTable).clear()
            self._files_cache.clear()
            self._trackers_cache.clear()
            self._files_torrent_id = None
            self._trackers_torrent_id = None

    async def _update_files_tab(self, torrent_id: int) -> None:
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != "files":
            return
        
        try:
            files = await self.controller.get_files(torrent_id)
            if files is None:
                return
            ft = self.query_one("#files-table", DataTable)

            if self._files_torrent_id != torrent_id:
                ft.clear()
                self._files_cache.clear()
                self._files_torrent_id = torrent_id

            desired_keys = [str(idx) for idx in sorted(files.keys())]

            # Remove missing rows
            for row in list(ft.ordered_rows):
                if row.key.value not in desired_keys:
                    ft.remove_row(row.key)
                    self._files_cache.pop(int(row.key.value), None)

            row_key_map = {row.key.value: row.key for row in ft.ordered_rows}
            row_obj_map = {row.key.value: row for row in ft.ordered_rows}

            for idx, f in sorted(files.items()):
                size = humanize.naturalsize(f.get("length", 0), binary=True)
                completed = f.get("bytesCompleted", 0)
                length = f.get("length", 1)
                percent = (completed / length) * 100 if length > 0 else 0
                pri = f.get("priority", 0)
                pri_icon = "⬆" if pri > 0 else ("⬇" if pri < 0 else "―")

                cells = (
                    Text(f.get("name", "Unknown"), overflow="ellipsis"),
                    Text(size, justify="right"),
                    Text(format_percent(percent), justify="right"),
                    Text(pri_icon, justify="center"),
                )
                snapshot = {
                    "name": f.get("name", "Unknown"),
                    "size": size,
                    "percent": round(percent, 2),
                    "priority": pri,
                }
                key_str = str(idx)
                row_key = row_key_map.get(key_str)
                if row_key is None:
                    row_key = ft.add_row(*cells, key=key_str)
                    row_key_map[key_str] = row_key
                else:
                    cached = self._files_cache.get(idx)
                    if cached != snapshot:
                        if cached is None or cached.get("name") != snapshot["name"]:
                            ft.update_cell(row_key, self._files_columns["name"], cells[0])
                        if cached is None or cached.get("size") != snapshot["size"]:
                            ft.update_cell(row_key, self._files_columns["size"], cells[1])
                        if cached is None or cached.get("percent") != snapshot["percent"]:
                            ft.update_cell(row_key, self._files_columns["done"], cells[2])
                        if cached is None or cached.get("priority") != snapshot["priority"]:
                            ft.update_cell(row_key, self._files_columns["priority"], cells[3])
                self._files_cache[idx] = snapshot

            # Keep original ordering based on file index
            self._sync_table_order(ft, desired_keys, row_obj_map)
        except Exception as exc:
            LOG.debug(f"Files tab update skipped: {exc}")

    async def _update_trackers_tab(self, torrent_id: int) -> None:
        """Update the trackers table for the selected torrent."""
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != "trackers":
            return
        
        try:
            trackers = await self.controller.get_trackers(torrent_id)
            tt = self.query_one("#trackers-table", DataTable)

            if self._trackers_torrent_id != torrent_id:
                tt.clear()
                self._trackers_cache.clear()
                self._trackers_torrent_id = torrent_id

            desired_keys = [f"{idx}-{t.get('host', 'unknown')}" for idx, t in enumerate(trackers)]

            for row in list(tt.ordered_rows):
                if row.key.value not in desired_keys:
                    tt.remove_row(row.key)
                    self._trackers_cache.pop(row.key.value, None)

            row_key_map = {row.key.value: row.key for row in tt.ordered_rows}
            row_obj_map = {row.key.value: row for row in tt.ordered_rows}

            for idx, tracker in enumerate(trackers):
                host = tracker.get("host", "unknown")
                if len(host) > 30:
                    host = host[:27] + "..."
                status = tracker.get("status", "")
                if len(status) > 15:
                    status = status[:12] + "..."
                
                if "success" in status.lower() or status == "":
                    status_text = Text(status or "OK", style="green")
                elif "error" in status.lower():
                    status_text = Text(status, style="red")
                else:
                    status_text = Text(status, style="yellow")
                
                cells = (
                    Text(host),
                    status_text,
                    Text(str(tracker.get("peers", 0)), justify="right"),
                    Text(str(tracker.get("seeders", 0)), justify="right"),
                    Text(str(tracker.get("leechers", 0)), justify="right"),
                )
                key_str = f"{idx}-{tracker.get('host', 'unknown')}"
                snapshot = {
                    "host": host,
                    "status": status_text.plain,
                    "peers": tracker.get("peers", 0),
                    "seeders": tracker.get("seeders", 0),
                    "leechers": tracker.get("leechers", 0),
                }
                row_key = row_key_map.get(key_str)
                if row_key is None:
                    row_key = tt.add_row(*cells, key=key_str)
                    row_key_map[key_str] = row_key
                else:
                    cached = self._trackers_cache.get(key_str)
                    if cached != snapshot:
                        if cached is None or cached.get("host") != snapshot["host"]:
                            tt.update_cell(row_key, self._trackers_columns["host"], cells[0])
                        if cached is None or cached.get("status") != snapshot["status"]:
                            tt.update_cell(row_key, self._trackers_columns["status"], cells[1])
                        if cached is None or cached.get("peers") != snapshot["peers"]:
                            tt.update_cell(row_key, self._trackers_columns["peers"], cells[2])
                        if cached is None or cached.get("seeders") != snapshot["seeders"]:
                            tt.update_cell(row_key, self._trackers_columns["seeders"], cells[3])
                        if cached is None or cached.get("leechers") != snapshot["leechers"]:
                            tt.update_cell(row_key, self._trackers_columns["leechers"], cells[4])
                self._trackers_cache[key_str] = snapshot

            self._sync_table_order(tt, desired_keys, row_obj_map)
        except Exception as exc:
            LOG.debug(f"Trackers tab update skipped: {exc}")

    def _render_disk_bar(self) -> None:
        disk_bar = self.query_one("#disk-bar", Static)
        if self.disk_total > 0:
            used = self.disk_total - self.disk_free
            bar = ProgressBar(
                total=float(self.disk_total),
                completed=float(used),
                width=15,
                complete_style="blue",
                finished_style="blue",
            )
            disk_bar.update(bar)

    def _append_speed(self, down: float, up: float) -> None:
        self._speed_down_hist.append(down)
        self._speed_up_hist.append(up)
        if len(self._speed_down_hist) > 60:
            self._speed_down_hist.pop(0)
            self._speed_up_hist.pop(0)
        self.query_one("Sparkline.-download", Sparkline).data = self._speed_down_hist
        self.query_one("Sparkline.-upload", Sparkline).data = self._speed_up_hist

    def _update_disk(self) -> None:
        try:
            usage = shutil.disk_usage(self.config.paths.download_dir)
            self.disk_free = usage.free
            self.disk_total = usage.total
        except Exception:
            self.disk_free = 0
            self.disk_total = 1

    # -------------------------------------------------------------------------
    # Filtering & Sorting
    # -------------------------------------------------------------------------

    def _apply_filter(self) -> None:
        text = self.filter_text.lower()
        self.filtered = []
        
        for t in self.torrents:
            if text and text not in t.name.lower():
                continue
            if self.status_filter_value == "active":
                if t.status not in ("downloading", "seeding", "checking"):
                    continue
            elif self.status_filter_value == "paused":
                if t.status not in ("stopped", "paused"):
                    continue
            elif self.status_filter_value == "error":
                if "error" not in t.status.lower():
                    continue
            if self.progress_filter_value == "done":
                if t.percent_done < 99.9:
                    continue
            elif self.progress_filter_value == "under50":
                if t.percent_done >= 50.0:
                    continue
            self.filtered.append(t)
        
        if self.selected_id is not None:
            if all(t.id != self.selected_id for t in self.filtered):
                self.selected_id = self.filtered[0].id if self.filtered else None

    def _sorted(self, data: list[TorrentView]) -> list[TorrentView]:
        if self.sort_column is None:
            return data
        key_funcs = {
            1: lambda t: t.id,
            2: lambda t: t.name.lower(),
            3: lambda t: t.percent_done,
            4: lambda t: t.eta,
            5: lambda t: t.rate_down,
            6: lambda t: t.rate_up,
            7: lambda t: t.ratio,
            8: lambda t: t.status,
        }
        key = key_funcs.get(self.sort_column, lambda t: t.id)
        return sorted(data, key=key, reverse=self.sort_desc)

    def _set_sort(self, col: int) -> None:
        if self.sort_column == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = col
            self.sort_desc = False
        self._render_table()

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self.query_one("#table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#table", DataTable).action_cursor_up()

    def action_cursor_bottom(self) -> None:
        self.query_one("#table", DataTable).action_scroll_end()

    async def action_refresh(self) -> None:
        await self.refresh_all()

    async def action_add(self) -> None:
        if not await self._check_connection():
            self.notify("❌ Not connected", severity="error")
            return

        async def handle_add_result(result: tuple[str, str] | None) -> None:
            if not result:
                return
            link, subdir = result
            link_path = Path(link).expanduser()
            if link_path.exists():
                link = str(link_path)
            
            # Validate download directory
            try:
                subdir = str(Path(subdir).expanduser().resolve())
            except Exception as e:
                self.notify(f"⚠️ Invalid path: {e}", severity="warning")
                return
            
            try:
                torrent = await self.controller.add(link, subdir)
                try:
                    await self.controller.start([torrent.id])
                except Exception:
                    # If already running or cannot start, ignore.
                    pass
                self.notify(f"➕ Added: {torrent.name[:30]}", severity="information")
                await self.refresh_all()
            except Exception as e:
                LOG.error(f"Failed to add torrent: {e}")
                self.notify(f"❌ Failed: {e}", severity="error")

        def on_dismiss(result: tuple[str, str] | None) -> None:
            if result:
                self.run_worker(handle_add_result(result))

        self._show_modal_with_callback(
            AddTorrentScreen(str(self.config.paths.download_dir)),
            on_dismiss
        )

    async def action_toggle(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            if torrent.status in {"downloading", "seeding", "checking"}:
                await self.controller.stop([torrent.id])
                self._user_paused.add(torrent.id)
                self.notify(f"⏸ Paused: {torrent.name[:20]}", severity="information")
            else:
                await self.controller.start([torrent.id])
                self._user_paused.discard(torrent.id)
                self.notify(f"▶ Started: {torrent.name[:20]}", severity="information")
            await self.refresh_all()
        except Exception as e:
            self.notify(f"❌ Error: {e}", severity="error")

    async def action_delete(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return

        async def _remove() -> None:
            try:
                await self.controller.remove([torrent.id], delete_data=True)
                self._completed_ids.discard(torrent.id)
                self._user_paused.discard(torrent.id)
                self._auto_started.discard(torrent.id)
                self.notify(f"🗑 Deleted: {torrent.name[:20]}", severity="warning")
                await self.refresh_all()
            except Exception as e:
                self.notify(f"❌ Error: {e}", severity="error")

        def _on_dismiss(result: bool | None) -> None:
            if result:
                self.run_worker(_remove())

        self._show_modal_with_callback(
            ConfirmScreen(f"Delete '{torrent.name}'?\n(Data will also be removed)"),
            _on_dismiss,
        )

    async def action_delete_keep(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return

        async def _remove() -> None:
            try:
                await self.controller.remove([torrent.id], delete_data=False)
                self._completed_ids.discard(torrent.id)
                self._user_paused.discard(torrent.id)
                self._auto_started.discard(torrent.id)
                self.notify(f"🗑 Deleted (kept data): {torrent.name[:20]}", severity="warning")
                await self.refresh_all()
            except Exception as e:
                self.notify(f"❌ Error: {e}", severity="error")

        def _on_dismiss(result: bool | None) -> None:
            if result:
                self.run_worker(_remove())

        self._show_modal_with_callback(
            ConfirmScreen(f"Delete '{torrent.name}'?\n(Data will be kept)"),
            _on_dismiss,
        )

    async def action_move(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        new_dir = await self._show_modal(MoveScreen(torrent.download_dir))
        if new_dir:
            try:
                # Validate path before attempting move
                expanded_path = Path(new_dir).expanduser()
                if not expanded_path.is_absolute():
                    self.notify(f"⚠️ Path must be absolute: {new_dir}", severity="warning")
                    return
                await self.controller.move([torrent.id], str(expanded_path), True)
                self.notify(f"📦 Moved to: {expanded_path}", severity="information")
                await self.refresh_all()
            except Exception as e:
                LOG.error(f"Move failed for torrent {torrent.id}: {e}")
                self.notify(f"❌ Error: {e}", severity="error")

    async def action_speed(self) -> None:
        if not await self._check_connection():
            return
        try:
            limits = await self.controller.get_speed_limits()
            result = await self._show_modal(SpeedScreen(limits["down"], limits["up"]))
            if result:
                down, up = result
                await self.controller.set_speed_limits(down, up)
                self.global_speed_limit_down = down
                self.global_speed_limit_up = up
                self._update_limit_badge()
                self._update_status_bar()
                self.notify(f"⚡ Speed: ↓{down} ↑{up} KiB/s", severity="information")
        except Exception as e:
            LOG.error(f"Failed to set global speed limits: {e}")
            self.notify(f"⚠️ Failed to set speed limits: {e}", severity="error")

    async def action_torrent_speed(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            limits = await self.controller.get_torrent_speed(torrent.id)
            result = await self._show_modal(SpeedScreen(limits["down"], limits["up"]))
            if result:
                down, up = result
                await self.controller.set_torrent_speed(torrent.id, down, up)
                self.notify(f"⚡ Torrent speed set", severity="information")
        except Exception as exc:
            LOG.error(f"Failed to set torrent speed: {exc}")
            self.notify(f"⚠️ Failed to set speed: {exc}", severity="error")

    async def action_priority(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            files = await self.controller.get_files(torrent.id)
            if not files:
                self.notify("⚠️ No files found for this torrent", severity="warning")
                return
            result = await self._show_modal(PriorityScreen(files))
            if result:
                high, normal, low = result
                await self.controller.set_priority(torrent.id, high, normal, low)
                self.notify("📋 Priorities updated", severity="information")
        except Exception as exc:
            LOG.error(f"Failed to set file priorities: {exc}")
            self.notify(f"⚠️ Failed to set priorities: {exc}", severity="error")

    async def action_verify(self) -> None:
        """Manual verify for the current torrent."""
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            await self.controller.verify([torrent.id])
            self._verified_ids.add(torrent.id)
            self.notify(f"🔎 Verified: {torrent.name[:30]}", severity="information")
            await self.refresh_all()
        except Exception as exc:
            self.notify(f"❌ Verify failed: {exc}", severity="error")

    async def action_filter(self) -> None:
        result = await self._show_modal(FilterScreen(self.filter_text))
        if result is not None:
            self.filter_text = result
            self._apply_filter()
            self._render_table()
            self._persist_ui()
            self._update_status_bar()

    async def action_status_filter(self) -> None:
        order = ["any", "active", "paused", "error"]
        idx = order.index(self.status_filter_value)
        self.status_filter_value = order[(idx + 1) % len(order)]
        self._apply_filter()
        self._render_table()
        self._persist_ui()
        self._update_status_bar()
        self.notify(f"Filter: {self.status_filter_value}", severity="information")

    async def action_progress_filter(self) -> None:
        order = ["any", "done", "under50"]
        idx = order.index(self.progress_filter_value)
        self.progress_filter_value = order[(idx + 1) % len(order)]
        self._apply_filter()
        self._render_table()
        self._persist_ui()
        self._update_status_bar()
        self.notify(f"Progress: {self.progress_filter_value}", severity="information")

    async def action_help(self) -> None:
        await self.push_screen(HelpScreen())

    def action_faster(self) -> None:
        self._set_refresh_interval(self.refresh_interval - 0.5)
        self.notify(f"Refresh: {self.refresh_interval:.1f}s", severity="information")

    def action_slower(self) -> None:
        self._set_refresh_interval(self.refresh_interval + 0.5)
        self.notify(f"Refresh: {self.refresh_interval:.1f}s", severity="information")

    def action_sort_1(self) -> None: self._set_sort(1)
    def action_sort_2(self) -> None: self._set_sort(2)
    def action_sort_3(self) -> None: self._set_sort(3)
    def action_sort_4(self) -> None: self._set_sort(4)
    def action_sort_5(self) -> None: self._set_sort(5)
    def action_sort_6(self) -> None: self._set_sort(6)
    def action_sort_7(self) -> None: self._set_sort(7)
    def action_sort_8(self) -> None: self._set_sort(8)

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.control.id == "table":
            col_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
            if event.column_index in col_map:
                self._set_sort(col_map[event.column_index])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.control.id == "table" and event.row_key:
            try:
                self.selected_id = int(event.row_key.value)
                self._render_details()
            except (ValueError, AttributeError):
                pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Refresh tab content when switching tabs."""
        if self.selected_id:
            if event.tab.id == "files":
                asyncio.create_task(self._update_files_tab(self.selected_id))
            elif event.tab.id == "trackers":
                asyncio.create_task(self._update_trackers_tab(self.selected_id))

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _current(self) -> TorrentView | None:
        if self.selected_id is None:
            return None
        return next((t for t in self.torrents if t.id == self.selected_id), None)

    def _show_modal_with_callback(self, screen: ModalScreen[T], callback: Any) -> None:
        """Show modal screen with callback on dismiss."""
        self._modal_depth += 1
        refresh_interval = self.refresh_interval

        def on_dismiss(result: T | None) -> None:
            try:
                self._modal_depth = max(0, self._modal_depth - 1)
                if self._modal_depth == 0:
                    try:
                        if self._refresh_timer is None:
                            self._refresh_timer = self.set_interval(refresh_interval, self.refresh_all)
                    except Exception as timer_exc:
                        LOG.debug(f"Failed to restart refresh timer: {timer_exc}")
                callback(result)
            except Exception as dismiss_exc:
                LOG.error(f"Modal dismiss callback error: {dismiss_exc}")
                # Ensure modal depth is restored even on error
                self._modal_depth = max(0, self._modal_depth - 1)

        self.push_screen(screen, callback=on_dismiss)

    async def _show_modal(self, screen: ModalScreen[T]) -> T | None:
        """Show modal screen and wait for result using callback + Future."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T | None] = loop.create_future()

        def _callback(result: T | None) -> None:
            if not future.done():
                future.set_result(result)

        self._show_modal_with_callback(screen, _callback)
        return await future

    def _persist_ui(self) -> None:
        self.config.ui.refresh_interval = self.refresh_interval
        self.config.ui.sort_column = self.sort_column
        self.config.ui.sort_desc = self.sort_desc
        self.config.ui.filter_text = self.filter_text
        self.config.ui.status_filter = self.status_filter_value
        self.config.ui.progress_filter = self.progress_filter_value
        try:
            save_config(self.config)
        except Exception:
            pass
