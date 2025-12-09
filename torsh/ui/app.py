"""
Torsh TUI Application - Enhanced Version

A polished, feature-rich Textual-based torrent client UI.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, TypeVar

from rich.progress_bar import ProgressBar
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Markdown,
    SelectionList,
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

LOG = get_logger(__name__)

T = TypeVar("T")


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
# Modal Screens
# =============================================================================

class BaseModalScreen(ModalScreen[T]):
    """Base class for modal screens."""
    pass


class AddTorrentScreen(BaseModalScreen[tuple[str, str] | None]):
    """Modal for adding a new torrent."""

    def __init__(self, download_dir: str) -> None:
        super().__init__()
        self.download_dir = download_dir

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="add-box"):
            yield Static("Add Torrent", classes="modal-title")
            yield Label("Magnet Link or File Path:")
            yield Input(
                placeholder="magnet:?xt=urn:btih:... or /path/to/file.torrent",
                id="link"
            )
            yield Label("Download Directory:")
            yield Input(value=self.download_dir, id="dir")
            with Horizontal(classes="buttons"):
                yield Button("Add", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        try:
            self.query_one("#link", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self._submit()
        else:
            self.dismiss(None)

    def _submit(self) -> None:
        link = self.query_one("#link", Input).value.strip()
        directory = self.query_one("#dir", Input).value.strip()
        if link:
            self.dismiss((link, directory or self.download_dir))
        else:
            self.dismiss(None)


class ConfirmScreen(BaseModalScreen[bool]):
    """Modal for yes/no confirmation."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="confirm-box"):
            yield Static("Confirmation", classes="modal-title")
            yield Label(self.message, classes="modal-label")
            with Horizontal(classes="buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class MoveScreen(BaseModalScreen[str | None]):
    """Modal for moving torrent data."""

    def __init__(self, current_dir: str) -> None:
        super().__init__()
        self.current_dir = current_dir

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="move-box"):
            yield Static("Move Data", classes="modal-title")
            yield Label("New Location:")
            yield Input(value=self.current_dir, id="newdir")
            with Horizontal(classes="buttons"):
                yield Button("Move", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            new_dir = self.query_one("#newdir", Input).value.strip()
            self.dismiss(new_dir or self.current_dir)
        else:
            self.dismiss(None)


class SpeedScreen(BaseModalScreen[tuple[int, int] | None]):
    """Modal for setting speed limits."""

    PRESETS = {
        "preset_off": (0, 0),
        "preset_stream": (8192, 2048),
        "preset_save": (256, 64),
    }

    def __init__(self, down: int, up: int) -> None:
        super().__init__()
        self.down = down
        self.up = up

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="speed-box"):
            yield Static("Speed Limits (KiB/s)", classes="modal-title")
            yield Label(f"Current: ↓ {self.down} / ↑ {self.up}")
            yield Input(value=str(self.down), placeholder="Download (0=unlimited)", id="down")
            yield Input(value=str(self.up), placeholder="Upload (0=unlimited)", id="up")
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="ok", variant="primary")
                yield Button("∞ Off", id="preset_off")
                yield Button("🎬 Stream", id="preset_stream")
                yield Button("💾 Save", id="preset_save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id in self.PRESETS:
            self.dismiss(self.PRESETS[btn_id])
            return
        if btn_id == "ok":
            try:
                down = int(self.query_one("#down", Input).value.strip() or "0")
                up = int(self.query_one("#up", Input).value.strip() or "0")
                self.dismiss((down, up))
            except ValueError:
                self.dismiss(None)
        else:
            self.dismiss(None)


class PriorityScreen(BaseModalScreen[tuple[list[int], list[int], list[int]] | None]):
    """Modal for setting file priorities."""

    def __init__(self, files: dict[int, dict[str, Any]]) -> None:
        super().__init__()
        self.files = files

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="prio-box"):
            yield Static("File Priority", classes="modal-title")
            options = [
                (f"{idx}: {info.get('name', 'Unknown')[:40]}", str(idx))
                for idx, info in self.files.items()
            ]
            yield Label("High Priority:")
            yield SelectionList[str](
                *[(label, key, False) for label, key in options],
                id="high"
            )
            yield Label("Low Priority:")
            yield SelectionList[str](
                *[(label, key, False) for label, key in options],
                id="low"
            )
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            self.dismiss(None)
            return
        high_list = self.query_one("#high", SelectionList)
        low_list = self.query_one("#low", SelectionList)
        high = [int(v) for v in high_list.selected_values]
        low = [int(v) for v in low_list.selected_values if v not in high_list.selected_values]
        normal = [int(k) for k in self.files.keys() if k not in high and k not in low]
        self.dismiss((high, normal, low))


class FilterScreen(BaseModalScreen[str | None]):
    """Modal for filtering torrents."""

    def __init__(self, current_filter: str) -> None:
        super().__init__()
        self.current_filter = current_filter

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="filter-box"):
            yield Static("Filter Torrents", classes="modal-title")
            yield Input(value=self.current_filter, placeholder="Filter by name...", id="flt")
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="ok", variant="primary")
                yield Button("Clear", id="clear")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#flt", Input).value.strip())
        elif event.button.id == "clear":
            self.dismiss("")
        else:
            self.dismiss(None)


class HelpScreen(BaseModalScreen[None]):
    """Modal displaying keyboard shortcuts."""

    HELP_TEXT = """
## Navigation
| Key | Action |
|-----|--------|
| `j` / `k` | Scroll Down/Up |
| `G` | Jump to Bottom |
| `Tab` | Switch Panes |

## Actions
| Key | Action |
|-----|--------|
| `a` | Add Torrent |
| `d` | Delete Torrent |
| `Space` | Pause/Resume |
| `r` | Refresh |
| `q` | Quit |

## Management
| Key | Action |
|-----|--------|
| `g` | Move Data |
| `s` | Global Speed |
| `t` | Torrent Speed |
| `p` | File Priorities |
| `/` | Filter by Name |
| `c` | Cycle Status Filter |
| `o` | Cycle Progress Filter |
"""

    def compose(self) -> ComposeResult:
        with Container(classes="modal-container", id="help-box"):
            yield Static("⌨️ Keyboard Shortcuts", classes="modal-title")
            yield Markdown(self.HELP_TEXT)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


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
        Binding("p", "priority", "Priority"),
        Binding("/", "filter", "Filter"),
        Binding("c", "status_filter", "Status"),
        Binding("o", "progress_filter", "Progress"),
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
        self._speed_down_hist: list[float] = [0.0] * 60
        self._speed_up_hist: list[float] = [0.0] * 60
        self._completed_ids: set[int] = set()  # Track completed torrents
        
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
            yield Label("TORSH", classes="app-title")
            yield Static("", id="status-bar")
            with Horizontal(id="header-stats"):
                with Horizontal(classes="stat-box"):
                    yield Label("Disk ", classes="stat-label")
                    yield Static("", id="disk-bar", markup=False)
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

        yield Footer()

    async def on_mount(self) -> None:
        # Setup main table
        table = self.query_one("#table", DataTable)
        table.add_columns("ID", "Name", "Progress", "ETA", "↓", "↑", "Ratio", "Status")
        
        # Setup files table
        files_table = self.query_one("#files-table", DataTable)
        files_table.add_columns("Name", "Size", "Done", "Pri")
        
        # Setup trackers table
        trackers_table = self.query_one("#trackers-table", DataTable)
        trackers_table.add_columns("Host", "Status", "Peers", "S", "L")

        # Initialize sparklines
        self.query_one("Sparkline.-download", Sparkline).data = self._speed_down_hist
        self.query_one("Sparkline.-upload", Sparkline).data = self._speed_up_hist
        
        self._set_refresh_interval(self.refresh_interval)
        await self.refresh_all()

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
        
        # Refresh rate
        parts.append(f"[dim]{self.refresh_interval:.1f}s[/]")
        
        status_bar = self.query_one("#status-bar", Static)
        status_bar.update(" │ ".join(parts))

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
        if not await self._check_connection():
            return
        await asyncio.gather(
            self._refresh_torrents(),
            self._refresh_stats(),
        )
        self._update_status_bar()

    async def _check_connection(self) -> bool:
        try:
            await self.controller.ensure_connected()
            self.connection_ok = True
            return True
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
                    return True
                except Exception:
                    pass
        self._update_status_bar()
        return False

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
            
            self._apply_filter()
            self._render_table()
        except Exception as exc:
            LOG.error(f"Refresh error: {exc}")

    async def _refresh_stats(self) -> None:
        try:
            stats = await self.controller.session_stats()
            self.download_speed = getattr(stats, "download_speed", 0) / 1024
            self.upload_speed = getattr(stats, "upload_speed", 0) / 1024
            self._append_speed(self.download_speed, self.upload_speed)
            self.active_count = getattr(stats, "active_torrent_count", 0)
            self.paused_count = getattr(stats, "paused_torrent_count", 0)
            self._update_disk()
            self._render_disk_bar()
        except Exception as exc:
            LOG.error(f"Stats error: {exc}")

    # -------------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------------

    def _render_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        
        data = self._sorted(self.filtered or self.torrents)
        
        for t in data:
            progress_bar = ProgressBar(total=100.0, completed=t.percent_done, width=10)
            table.add_row(
                Text(str(t.id), justify="right"),
                Text(t.name, overflow="ellipsis", no_wrap=True),
                progress_bar,
                Text(t.eta, justify="right"),
                Text(t.rate_down, style="green" if t.rate_down != "0 B/s" else "dim", justify="right"),
                Text(t.rate_up, style="blue" if t.rate_up != "0 B/s" else "dim", justify="right"),
                styled_ratio(t.ratio),
                styled_status(t.status),
                key=str(t.id),
            )
        
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

    async def _update_files_tab(self, torrent_id: int) -> None:
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != "files":
            return
        
        try:
            files = await self.controller.get_files(torrent_id)
            if not files:
                return
            
            ft = self.query_one("#files-table", DataTable)
            if ft.row_count != len(files):
                ft.clear()
            
            if ft.row_count == 0:
                for idx, f in files.items():
                    size = humanize.naturalsize(f.get("length", 0), binary=True)
                    completed = f.get("bytesCompleted", 0)
                    length = f.get("length", 1)
                    percent = (completed / length) * 100 if length > 0 else 0
                    pri = f.get("priority", 0)
                    pri_icon = "⬆" if pri > 0 else ("⬇" if pri < 0 else "―")
                    ft.add_row(
                        Text(f.get("name", "Unknown"), overflow="ellipsis"),
                        Text(size, justify="right"),
                        Text(format_percent(percent), justify="right"),
                        Text(pri_icon, justify="center"),
                    )
        except Exception:
            pass

    async def _update_trackers_tab(self, torrent_id: int) -> None:
        """Update the trackers table for the selected torrent."""
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != "trackers":
            return
        
        try:
            trackers = await self.controller.get_trackers(torrent_id)
            tt = self.query_one("#trackers-table", DataTable)
            tt.clear()
            
            for t in trackers:
                host = t.get("host", "unknown")
                if len(host) > 30:
                    host = host[:27] + "..."
                status = t.get("status", "")
                if len(status) > 15:
                    status = status[:12] + "..."
                
                # Color status
                if "success" in status.lower() or status == "":
                    status_text = Text(status or "OK", style="green")
                elif "error" in status.lower():
                    status_text = Text(status, style="red")
                else:
                    status_text = Text(status, style="yellow")
                
                tt.add_row(
                    Text(host),
                    status_text,
                    Text(str(t.get("peers", 0)), justify="right"),
                    Text(str(t.get("seeders", 0)), justify="right"),
                    Text(str(t.get("leechers", 0)), justify="right"),
                )
        except Exception:
            pass

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
            return
        result = await self._show_modal(AddTorrentScreen(str(self.config.paths.download_dir)))
        if not result:
            return
        link, subdir = result
        link_path = Path(link).expanduser()
        if link_path.exists():
            link = str(link_path)
        subdir = str(Path(subdir).expanduser())
        try:
            await self.controller.add(link, subdir)
            self.notify(f"➕ Added torrent", severity="information")
            await self.refresh_all()
        except Exception as e:
            self.notify(f"❌ Failed: {e}", severity="error")

    async def action_toggle(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            if torrent.status in {"downloading", "seeding", "checking"}:
                await self.controller.stop([torrent.id])
                self.notify(f"⏸ Paused: {torrent.name[:20]}", severity="information")
            else:
                await self.controller.start([torrent.id])
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
        confirm = await self._show_modal(ConfirmScreen(f"Delete '{torrent.name}'?\n(Data will also be removed)"))
        if confirm:
            try:
                await self.controller.remove([torrent.id], delete_data=True)
                self._completed_ids.discard(torrent.id)
                self.notify(f"🗑 Deleted: {torrent.name[:20]}", severity="warning")
                await self.refresh_all()
            except Exception as e:
                self.notify(f"❌ Error: {e}", severity="error")

    async def action_move(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        new_dir = await self._show_modal(MoveScreen(torrent.download_dir))
        if new_dir:
            try:
                await self.controller.move([torrent.id], new_dir, True)
                self.notify(f"📦 Moved to: {new_dir}", severity="information")
                await self.refresh_all()
            except Exception as e:
                self.notify(f"❌ Error: {e}", severity="error")

    async def action_speed(self) -> None:
        if not await self._check_connection():
            return
        limits = await self.controller.get_speed_limits()
        result = await self._show_modal(SpeedScreen(limits["down"], limits["up"]))
        if result:
            down, up = result
            await self.controller.set_speed_limits(down, up)
            self.notify(f"⚡ Speed: ↓{down} ↑{up} KiB/s", severity="information")

    async def action_torrent_speed(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        limits = await self.controller.get_torrent_speed(torrent.id)
        result = await self._show_modal(SpeedScreen(limits["down"], limits["up"]))
        if result:
            down, up = result
            await self.controller.set_torrent_speed(torrent.id, down, up)
            self.notify(f"⚡ Torrent speed set", severity="information")

    async def action_priority(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        files = await self.controller.get_files(torrent.id)
        if not files:
            return
        result = await self._show_modal(PriorityScreen(files))
        if result:
            high, normal, low = result
            await self.controller.set_priority(torrent.id, high, normal, low)
            self.notify("📋 Priorities updated", severity="information")

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
            if event.tab.id == "files-tab":
                asyncio.create_task(self._update_files_tab(self.selected_id))
            elif event.tab.id == "trackers-tab":
                asyncio.create_task(self._update_trackers_tab(self.selected_id))

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _current(self) -> TorrentView | None:
        if self.selected_id is None:
            return None
        return next((t for t in self.torrents if t.id == self.selected_id), None)

    async def _show_modal(self, screen: ModalScreen[T]) -> T | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T | None] = loop.create_future()
        original_dismiss = screen.dismiss

        def wrapped_dismiss(result: T | None = None) -> None:
            if not future.done():
                future.set_result(result)
            original_dismiss(result)

        screen.dismiss = wrapped_dismiss  # type: ignore
        try:
            await self.push_screen(screen)
            return await future
        except Exception:
            return None

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
