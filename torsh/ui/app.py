from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Markdown, RichLog, SelectionList, Static
from transmission_rpc.error import TransmissionError

import humanize

from ..client import TorrentView, TransmissionController
from ..config import AppConfig, save_config
from ..daemon import maybe_start_daemon
from ..logging import get_logger


LOG = get_logger(__name__)


def _fmt_percent(value: float) -> str:
    return f"{value:5.1f}%"


def _status_label(status: str) -> str:
    mapping = {
        "downloading": "⬇️  Downloading",
        "seeding": "⬆️  Seeding",
        "stopped": "⏸  Stopped",
        "paused": "⏸  Paused",
        "checking": "🔎 Checking",
        "queued": "⏳ Queued",
    }
    return mapping.get(status, status)


class AddTorrentScreen(ModalScreen[tuple[str, str] | None]):
    DEFAULT_CSS = """
    AddTorrentScreen {
        align: center middle;
    }
    #add-box {
        border: tall $accent;
        width: 80%;
        max-width: 100;
        background: $panel;
        padding: 1 2;
    }
    """

    def __init__(self, download_dir: str):
        super().__init__()
        self.download_dir = download_dir

    def compose(self) -> ComposeResult:
        with Container(id="add-box"):
            yield Static("Add torrent or magnet", classes="title")
            yield Label("Link or path to .torrent")
            yield Input(placeholder="magnet:?xt=urn:btih:... or /path/file.torrent", id="link")
            yield Label("Download directory")
            yield Input(value=self.download_dir, id="dir")
            with Horizontal(classes="buttons"):
                yield Button("Add", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            link = self.query_one("#link", Input).value.strip()
            directory = self.query_one("#dir", Input).value.strip()
            if link:
                self.dismiss((link, directory or self.download_dir))
            else:
                self.dismiss(None)
        else:
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(self.text)
            with Horizontal(classes="buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class MoveScreen(ModalScreen[str | None]):
    def __init__(self, current: str):
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="move-box"):
            yield Static("New download directory")
            yield Input(value=self.current, id="newdir")
            with Horizontal(classes="buttons"):
                yield Button("Move", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            new_dir = self.query_one("#newdir", Input).value.strip()
            self.dismiss(new_dir or self.current)
        else:
            self.dismiss(None)


class SpeedScreen(ModalScreen[tuple[int | None, int | None] | None]):
    def __init__(self, down: int, up: int):
        super().__init__()
        self.down = down
        self.up = up

    def compose(self) -> ComposeResult:
        with Container(id="speed-box"):
            yield Static("Speed limits (KiB/s). 0 or empty = disable")
            yield Label(f"Current: ↓ {self.down} / ↑ {self.up}")
            yield Input(value=str(self.down), placeholder="down KiB/s", id="down")
            yield Input(value=str(self.up), placeholder="up KiB/s", id="up")
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="ok", variant="primary")
                yield Button("No limit", id="preset_off")
                yield Button("Stream", id="preset_stream")
                yield Button("Save", id="preset_save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "ok":
            if event.button.id == "preset_off":
                self.dismiss((0, 0))
                return
            if event.button.id == "preset_stream":
                self.dismiss((8192, 2048))  # ~8 MiB/s down, ~2 MiB/s up
                return
            if event.button.id == "preset_save":
                self.dismiss((256, 64))  # modest saving profile
                return
            self.dismiss(None)
            return
        try:
            down_raw = self.query_one("#down", Input).value.strip()
            up_raw = self.query_one("#up", Input).value.strip()
            down = int(down_raw) if down_raw else 0
            up = int(up_raw) if up_raw else 0
            self.dismiss((down, up))
        except ValueError:
            self.dismiss(None)


class PriorityScreen(ModalScreen[tuple[list[int], list[int], list[int]] | None]):
    def __init__(self, files: dict[int, dict]):
        super().__init__()
        self.files = files

    def compose(self) -> ComposeResult:
        with Container(id="prio-box"):
            yield Static("Select High / Low. Not selected => Normal.")
            options = [(f"{idx}: {info.get('name','')}", str(idx)) for idx, info in self.files.items()]
            yield Label("High priority")
            yield SelectionList[str](*[(label, key, False) for label, key in options], id="high")
            yield Label("Low priority")
            yield SelectionList[str](*[(label, key, False) for label, key in options], id="low")
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


class LogScreen(ModalScreen[None]):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self._timer = None

    def compose(self) -> ComposeResult:
        with Container(id="log-box"):
            yield Static(f"daemon log: {self.path}")
            yield Markdown(self._read_tail(), id="daemon-log")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self._timer = self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        md = self.query_one("#daemon-log", Markdown)
        md.update(self._read_tail())

    def _read_tail(self) -> str:
        if not self.path.exists():
            return "_no log file yet_"
        lines = self.path.read_text(errors="ignore").splitlines()
        tail = "\n".join(lines[-200:])
        return f"```\n{tail}\n```"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class FilterScreen(ModalScreen[str | None]):
    def __init__(self, current: str):
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="filter-box"):
            yield Static("Filter by name (case-insensitive)")
            yield Input(value=self.current, placeholder="text to match", id="flt")
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


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Static("Controls")
            help_md = """
`a` add · `space` pause/resume · `d` delete · `g` move · `r` refresh
`s` speed limits · `t` torrent speed · `p` file priorities · `l` daemon log
`/` filter by name · `c` status filter · `o` progress filter
`1..8` sort by column · `]` faster · `[` slower · `?` help · `q` quit
"""
            yield Markdown(help_md)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class TorshApp(App):
    CSS_PATH = "ui/styles.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("space", "toggle", "Pause/Start"),
        Binding("r", "refresh", "Refresh"),
        Binding("g", "move", "Move dir"),
        Binding("s", "speed", "Global limits"),
        Binding("t", "torrent_speed", "Torrent limit"),
        Binding("p", "priority", "File priority"),
        Binding("l", "log", "Daemon log"),
        Binding("/", "filter", "Filter"),
        Binding("c", "status_filter", "Status filter"),
        Binding("o", "progress_filter", "Progress filter"),
        Binding("]", "faster", "Faster"),
        Binding("[", "slower", "Slower"),
        Binding("?", "help", "Help"),
        Binding("1", "sort1", "Sort ID"),
        Binding("2", "sort2", "Sort Name"),
        Binding("3", "sort3", "Sort %"),
        Binding("4", "sort4", "Sort ETA"),
        Binding("5", "sort5", "Sort ↓"),
        Binding("6", "sort6", "Sort ↑"),
        Binding("7", "sort7", "Sort Ratio"),
        Binding("8", "sort8", "Sort Status"),
    ]

    download_speed = reactive("0 B/s")
    upload_speed = reactive("0 B/s")
    active_count = reactive(0)
    paused_count = reactive(0)
    connection_issue = reactive[str | None](None)
    refresh_interval = reactive(2.5)
    sort_column = reactive[int | None](None)
    sort_desc = reactive(False)
    status_filter_value = reactive("any")  # any, active, paused, error
    progress_filter_value = reactive("any")  # any, done, under50
    disk_free = reactive("n/a")
    disk_total = reactive("n/a")

    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.controller = TransmissionController(config)
        self.torrents: list[TorrentView] = []
        self.filtered: list[TorrentView] = []
        self.selected_id: Optional[int] = None
        self.filter_text: str = config.ui.filter_text
        self._refresh_timer = None
        self._speed_down_hist: list[float] = []
        self._speed_up_hist: list[float] = []
        # restore UI state
        self.refresh_interval = config.ui.refresh_interval
        self.sort_column = config.ui.sort_column
        self.sort_desc = config.ui.sort_desc
        self.status_filter_value = config.ui.status_filter
        self.progress_filter_value = config.ui.progress_filter

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            with Horizontal():
                with Vertical(id="left"):
                    yield Static("Torrents", classes="panel-title")
                    yield DataTable(id="table", zebra_stripes=True)
                    yield RichLog(id="log", highlight=True, markup=True)
                with Vertical(id="right"):
                    yield Static("Details", classes="panel-title")
                    yield Markdown("", id="details", code_theme="dracula")
                    yield Markdown("", id="graphs")
                    yield Static(id="stats")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("ID", "Name", "Done", "ETA", "↓", "↑", "Ratio", "Status")
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._set_refresh_interval(self.refresh_interval)
        await self.refresh_all()

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
        await asyncio.gather(self.refresh_torrents(), self.refresh_stats())

    async def _check_connection(self) -> bool:
        try:
            await self.controller.ensure_connected()
            if self.connection_issue:
                self._log("[green]Connection restored[/]")
            self.connection_issue = None
            return True
        except TransmissionError as exc:
            self.connection_issue = f"No Transmission RPC connection: {exc}"
        except Exception as exc:  # noqa: BLE001
            self.connection_issue = f"Connection error: {exc}"
            if self.config.daemon.restart_on_fail and self.config.daemon.autostart:
                maybe_start_daemon(self.config)
                await asyncio.sleep(1.5)
                try:
                    await self.controller.ensure_connected()
                    self.connection_issue = None
                    self._log("[yellow]Daemon restarted, connection restored[/]")
                    return True
                except Exception:
                    pass
        self._render_stats()
        self._log(f"[red]{self.connection_issue}[/]")
        return False

    async def refresh_torrents(self) -> None:
        try:
            self.torrents = await self.controller.list_torrents()
            self._apply_filter()
            self._render_table()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Refresh error: {exc}[/]")

    async def refresh_stats(self) -> None:
        try:
            stats = await self.controller.session_stats()
            dl_kib = stats.download_speed / 1024
            ul_kib = stats.upload_speed / 1024
            self.download_speed = f"{dl_kib:.1f} KiB/s"
            self.upload_speed = f"{ul_kib:.1f} KiB/s"
            self._append_speed(dl_kib, ul_kib)
            self.active_count = stats.active_torrent_count
            self.paused_count = stats.paused_torrent_count
            self._update_disk()
            self._render_stats()
            self._render_graphs()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Stats error: {exc}[/]")

    def _render_table(self) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        data = self._sorted(self.filtered or self.torrents)
        for t in data:
            table.add_row(
                str(t.id),
                t.name,
                _fmt_percent(t.percent_done),
                t.eta,
                t.rate_down,
                t.rate_up,
                f"{t.ratio:.2f}",
                _status_label(t.status),
                key=str(t.id),
            )
        # Restore selection
        if self.selected_id is not None:
            try:
                table.cursor_coordinate = (self._row_index(self.selected_id), 0)
            except Exception:
                self.selected_id = None
        if self.selected_id is None and data:
            self.selected_id = data[0].id
            table.cursor_coordinate = (0, 0)
        self._render_details()

    def _row_index(self, torrent_id: int) -> int:
        data = self._sorted(self.filtered or self.torrents)
        for idx, t in enumerate(data):
            if t.id == torrent_id:
                return idx
        return 0

    def _render_details(self) -> None:
        details = self.query_one("#details", Markdown)
        data = self._sorted(self.filtered or self.torrents)
        torrent = next((t for t in data if t.id == self.selected_id), None)
        if not torrent:
            details.update("_Nothing selected_")
            return
        md = f"""
**{torrent.name}**

- Status: `{_status_label(torrent.status)}`
- Done: `{_fmt_percent(torrent.percent_done)}`
- ETA: `{torrent.eta}`
- Speed: `↓ {torrent.rate_down}` / `↑ {torrent.rate_up}`
- Size: `{torrent.size}`, Ratio: `{torrent.ratio:.2f}`
- Peers: `{torrent.peers}` (S:{torrent.seeders}/L:{torrent.leechers})
- Path: `{torrent.download_dir}`
"""
        details.update(md)

    def _render_stats(self) -> None:
        stats = self.query_one("#stats", Static)
        if self.connection_issue:
            stats.update(f"[red]{self.connection_issue}[/]")
        else:
            stats.update(
                f"[b]↓ {self.download_speed}[/] {self._spark(self._speed_down_hist)} | [b]↑ {self.upload_speed}[/] {self._spark(self._speed_up_hist)} · Active: {self.active_count} · Paused: {self.paused_count} · Refresh {self.refresh_interval:.1f}s · Sort {self._sort_label()} · Status {self.status_filter_value} · Progress {self.progress_filter_value} · Disk {self.disk_free}/{self.disk_total}"
            )

    def _render_graphs(self) -> None:
        graphs = self.query_one("#graphs", Markdown)
        md = f"""
**Speeds**
- Down: `{self.download_speed}` {self._spark(self._speed_down_hist)}
- Up: `{self.upload_speed}` {self._spark(self._speed_up_hist)}

**Disk**
- Free: `{self.disk_free}` / Total: `{self.disk_total}`
"""
        graphs.update(md)

    def _log(self, message: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(message)

    async def action_refresh(self) -> None:
        await self.refresh_all()

    async def action_add(self) -> None:
        if not await self._check_connection():
            return
        result = await self.push_screen_wait(AddTorrentScreen(str(self.config.paths.download_dir)))
        if not result:
            return
        link, directory = result
        try:
            await self.controller.add(link, directory)
            self._log(f"[green]Added:[/] {link}")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Add failed: {exc}[/]")

    async def action_toggle(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        try:
            if torrent.status in {"downloading", "seeding", "checking"}:
                await self.controller.stop([torrent.id])
                self._log(f"[yellow]Paused:[/] {torrent.name}")
            else:
                await self.controller.start([torrent.id])
                self._log(f"[green]Started:[/] {torrent.name}")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]State change failed: {exc}[/]")

    async def action_delete(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        confirm = await self.push_screen_wait(ConfirmScreen(f"Delete {torrent.name} (data too)?"))
        if confirm is None:
            return
        try:
            await self.controller.remove([torrent.id], delete_data=confirm)
            self._log(f"[red]Deleted:[/] {torrent.name} (data: {'yes' if confirm else 'no'})")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Delete failed: {exc}[/]")

    async def action_move(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        new_dir = await self.push_screen_wait(MoveScreen(torrent.download_dir))
        if not new_dir:
            return
        try:
            Path(new_dir).expanduser().mkdir(parents=True, exist_ok=True)
            await self.controller.move([torrent.id], location=new_dir, move_data=True)
            self._log(f"[cyan]Moved to:[/] {new_dir}")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Move failed: {exc}[/]")

    async def action_speed(self) -> None:
        if not await self._check_connection():
            return
        limits = await self.controller.get_speed_limits()
        result = await self.push_screen_wait(SpeedScreen(limits["down"], limits["up"]))
        if not result:
            return
        down, up = result
        try:
            await self.controller.set_speed_limits(down, up)
            self._log(f"[cyan]Global limits:[/] ↓ {down} KiB/s, ↑ {up} KiB/s")
            await self.refresh_stats()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Speed limit error: {exc}[/]")

    async def action_priority(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        files = await self.controller.get_files(torrent.id)
        if not files:
            self._log("[yellow]No files to reprioritize[/]")
            return
        result = await self.push_screen_wait(PriorityScreen(files))
        if not result:
            return
        high, normal, low = result
        try:
            await self.controller.set_priority(torrent.id, high, normal, low)
            self._log(f"[cyan]Priorities updated[/]")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Priority error: {exc}[/]")

    async def action_log(self) -> None:
        await self.push_screen(LogScreen(self.config.daemon.log_path))

    async def action_torrent_speed(self) -> None:
        if not await self._check_connection():
            return
        torrent = self._current()
        if not torrent:
            return
        limits = await self.controller.get_torrent_speed(torrent.id)
        result = await self.push_screen_wait(SpeedScreen(limits["down"], limits["up"]))
        if not result:
            return
        down, up = result
        try:
            await self.controller.set_torrent_speed(torrent.id, down, up)
            self._log(f"[cyan]Torrent limit:[/] ↓ {down} KiB/s, ↑ {up} KiB/s")
            await self.refresh_all()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[red]Torrent limit error: {exc}[/]")

    async def action_filter(self) -> None:
        result = await self.push_screen_wait(FilterScreen(self.filter_text))
        if result is None:
            return
        self.filter_text = result
        self._apply_filter()
        self._render_table()
        self._persist_ui()

    async def action_faster(self) -> None:
        self._set_refresh_interval(self.refresh_interval - 0.5)
        self._render_stats()
        self._log(f"[cyan]Refresh {self.refresh_interval:.1f}s[/]")
        self._persist_ui()

    async def action_slower(self) -> None:
        self._set_refresh_interval(self.refresh_interval + 0.5)
        self._render_stats()
        self._log(f"[cyan]Refresh {self.refresh_interval:.1f}s[/]")
        self._persist_ui()

    async def action_help(self) -> None:
        await self.push_screen(HelpScreen())

    def _apply_filter(self) -> None:
        if not self.filter_text:
            ft_pred = lambda _: True
        else:
            ft = self.filter_text.lower()
            ft_pred = lambda t: ft in t.name.lower()

        def status_pred(t: TorrentView) -> bool:
            if self.status_filter_value == "active":
                return t.status in {"downloading", "seeding", "checking"}
            if self.status_filter_value == "paused":
                return t.status in {"stopped", "paused"}
            if self.status_filter_value == "error":
                return "error" in t.status.lower()
            return True

        def progress_pred(t: TorrentView) -> bool:
            if self.progress_filter_value == "done":
                return t.percent_done >= 99.9
            if self.progress_filter_value == "under50":
                return t.percent_done < 50.0
            return True

        self.filtered = [t for t in self.torrents if ft_pred(t) and status_pred(t) and progress_pred(t)]
        if self.selected_id is not None and all(t.id != self.selected_id for t in self.filtered):
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

    async def action_sort1(self) -> None:
        self._set_sort(1)

    async def action_sort2(self) -> None:
        self._set_sort(2)

    async def action_sort3(self) -> None:
        self._set_sort(3)

    async def action_sort4(self) -> None:
        self._set_sort(4)

    async def action_sort5(self) -> None:
        self._set_sort(5)

    async def action_sort6(self) -> None:
        self._set_sort(6)

    async def action_sort7(self) -> None:
        self._set_sort(7)

    async def action_sort8(self) -> None:
        self._set_sort(8)

    def _set_sort(self, col: int) -> None:
        if self.sort_column == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = col
            self.sort_desc = False
        self._render_table()
        self._persist_ui()

    def _sort_label(self) -> str:
        if self.sort_column is None:
            return "none"
        names = {
            1: "ID",
            2: "Name",
            3: "%",
            4: "ETA",
            5: "↓",
            6: "↑",
            7: "Ratio",
            8: "Status",
        }
        direction = "desc" if self.sort_desc else "asc"
        return f"{names.get(self.sort_column,'')} {direction}"

    def _persist_ui(self) -> None:
        # Update config UI state and save (best-effort)
        self.config.ui.refresh_interval = self.refresh_interval
        self.config.ui.sort_column = self.sort_column
        self.config.ui.sort_desc = self.sort_desc
        self.config.ui.filter_text = self.filter_text
        self.config.ui.status_filter = self.status_filter_value
        self.config.ui.progress_filter = self.progress_filter_value
        try:
            save_config(self.config)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[yellow]Config save failed: {exc}[/]")

    async def action_status_filter(self) -> None:
        order = ["any", "active", "paused", "error"]
        idx = order.index(self.status_filter_value)
        self.status_filter_value = order[(idx + 1) % len(order)]
        self._log(f"[cyan]Status filter:[/] {self.status_filter_value}")
        self._apply_filter()
        self._render_table()
        self._persist_ui()

    async def action_progress_filter(self) -> None:
        order = ["any", "done", "under50"]
        idx = order.index(self.progress_filter_value)
        self.progress_filter_value = order[(idx + 1) % len(order)]
        self._log(f"[cyan]Progress filter:[/] {self.progress_filter_value}")
        self._apply_filter()
        self._render_table()
        self._persist_ui()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # Map visible column index to sort key (1-based as in bindings)
        self._set_sort(event.column_index + 1)

    def _append_speed(self, down_kib: float, up_kib: float) -> None:
        def push(buf: list[float], val: float):
            buf.append(val)
            if len(buf) > 40:
                buf.pop(0)

        push(self._speed_down_hist, down_kib)
        push(self._speed_up_hist, up_kib)

    def _spark(self, buf: list[float]) -> str:
        if not buf:
            return ""
        blocks = "▁▂▃▄▅▆▇█"
        mn, mx = min(buf), max(buf)
        if mx == mn:
            return blocks[0] * min(len(buf), 12)
        scaled = []
        for v in buf[-12:]:
            idx = int((v - mn) / (mx - mn) * (len(blocks) - 1))
            scaled.append(blocks[idx])
        return "".join(scaled)

    def _update_disk(self) -> None:
        try:
            usage = shutil.disk_usage(self.config.paths.download_dir)
            self.disk_free = humanize.naturalsize(usage.free, binary=True)
            self.disk_total = humanize.naturalsize(usage.total, binary=True)
        except Exception:
            self.disk_free = "n/a"
            self.disk_total = "n/a"

    def _current(self) -> TorrentView | None:
        if self.selected_id is None:
            return None
        return next((t for t in self.torrents if t.id == self.selected_id), None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            self.selected_id = int(event.row_key)
        except Exception:
            self.selected_id = None
        self._render_details()

    def on_resize(self, event: events.Resize) -> None:
        # Refresh details on resize so markdown wraps correctly
        self._render_details()


