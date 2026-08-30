"""Modal dialogs for the torsh TUI."""

from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
    Switch,
    TextArea,
)

from ..units import fmt_size, parse_size


class TorshModal(ModalScreen):
    """Base modal: Escape cancels, dialog box styling."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddTorrentModal(TorshModal):
    """Paste one or many magnet links / .torrent paths / URLs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+s", "submit", "Add", priority=True),
    ]

    def __init__(self, download_dir: str, sequential_default: bool) -> None:
        super().__init__()
        self._download_dir = download_dir
        self._sequential = sequential_default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Add torrents", classes="dialog-title")
            yield Label("Magnet links, .torrent files or URLs — one per line:")
            yield TextArea(id="uris", show_line_numbers=False)
            yield Label("Download to:")
            yield Input(value=self._download_dir, id="dir")
            with Horizontal(classes="switch-row"):
                yield Switch(value=False, id="paused")
                yield Label("start paused", classes="switch-label")
                yield Switch(value=self._sequential, id="sequential")
                yield Label("sequential (streaming)", classes="switch-label")
            with Horizontal(classes="buttons"):
                yield Button("Add  [dim]ctrl+s[/]", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#uris", TextArea).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        text = self.query_one("#uris", TextArea).text
        uris = [line.strip() for line in text.splitlines() if line.strip()]
        if not uris:
            self.query_one("#uris", TextArea).focus()
            return
        self.dismiss(
            {
                "uris": uris,
                "dir": self.query_one("#dir", Input).value.strip() or self._download_dir,
                "paused": self.query_one("#paused", Switch).value,
                "sequential": self.query_one("#sequential", Switch).value,
            }
        )


class RemoveModal(TorshModal):
    """Remove torrents; choose whether the downloaded files go too."""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._names = names

    def compose(self) -> ComposeResult:
        shown = "\n".join(f"  • {n[:56]}" for n in self._names[:5])
        if len(self._names) > 5:
            shown += f"\n  … and {len(self._names) - 5} more"
        with Vertical(classes="dialog"):
            yield Static(f"Remove {len(self._names)} torrent(s)?", classes="dialog-title")
            yield Static(shown, classes="dialog-body")
            with RadioSet(id="mode"):
                yield RadioButton("Keep downloaded files", value=True, id="keep")
                yield RadioButton("Delete files from disk", id="delete")
            with Horizontal(classes="buttons"):
                yield Button("Remove", variant="error", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        delete = self.query_one("#delete", RadioButton).value
        self.dismiss("data" if delete else "keep")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class LimitsModal(TorshModal):
    """Speed limits with human units; empty/'off' means unlimited."""

    def __init__(self, title: str, down: int, up: int) -> None:
        super().__init__()
        self._title = title
        self._down = down
        self._up = up

    @staticmethod
    def _prefill(value: int) -> str:
        return fmt_size(value) if value > 0 else "off"

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self._title, classes="dialog-title")
            yield Label("Download limit (e.g. 2M, 500K, off):")
            yield Input(value=self._prefill(self._down), id="down")
            yield Label("Upload limit:")
            yield Input(value=self._prefill(self._up), id="up")
            yield Static("", id="limit-error", classes="dialog-error")
            with Horizontal(classes="buttons"):
                yield Button("Apply", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#down", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self, event: Any = None) -> None:
        down_text = self.query_one("#down", Input).value.strip()
        up_text = self.query_one("#up", Input).value.strip()
        try:
            # An untouched field keeps the exact current limit — re-parsing
            # the rounded prefill ("1.2 MiB") would silently drift the value.
            down = self._down if down_text == self._prefill(self._down) else parse_size(down_text or "off")
            up = self._up if up_text == self._prefill(self._up) else parse_size(up_text or "off")
        except ValueError as exc:
            self.query_one("#limit-error", Static).update(f"✗ {exc}")
            return
        self.dismiss((down, up))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class MoveModal(TorshModal):
    """Move a torrent's data to a new directory."""

    def __init__(self, name: str, current: str) -> None:
        super().__init__()
        # Deliberately not `self._name`: Textual's DOMNode already owns that
        # attribute (it backs the public `name` property and the DOM repr).
        self._torrent_name = name
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("Move data", classes="dialog-title")
            yield Static(self._torrent_name[:60], classes="dialog-body")
            yield Label("New location:")
            yield Input(value=self._current, id="dir")
            with Horizontal(classes="buttons"):
                yield Button("Move", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#dir", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self, event: Any = None) -> None:
        value = self.query_one("#dir", Input).value.strip()
        self.dismiss(value or None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class PrioritiesModal(TorshModal):
    """Per-file priorities: move the cursor, press h/n/l/s, apply."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("h", "set_priority('high')", "High"),
        Binding("n", "set_priority('normal')", "Normal"),
        Binding("l", "set_priority('low')", "Low"),
        Binding("s", "set_priority('skip')", "Skip"),
        Binding("enter", "apply", "Apply", priority=True),
    ]

    def __init__(self, name: str, files: list[dict[str, Any]]) -> None:
        super().__init__()
        self._torrent_name = name
        self._files = files
        self._changes: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog dialog-wide"):
            yield Static(f"File priorities — {self._torrent_name[:48]}", classes="dialog-title")
            yield Static(
                "[b]h[/]igh · [b]n[/]ormal · [b]l[/]ow · [b]s[/]kip — Enter applies",
                classes="dialog-body",
            )
            yield DataTable(id="prio-table", cursor_type="row")
            with Horizontal(classes="buttons"):
                yield Button("Apply", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#prio-table", DataTable)
        table.add_columns("#", "file", "size", "done", "priority")
        for f in self._files:
            table.add_row(
                str(f["index"]),
                f["path"][-48:],
                fmt_size(f["size"]),
                f"{f['progress'] * 100:.0f}%",
                f["priority_label"],
                key=str(f["index"]),
            )
        table.focus()

    def action_set_priority(self, label: str) -> None:
        table = self.query_one("#prio-table", DataTable)
        if not table.row_count:
            return
        row = table.cursor_coordinate.row
        key = str(self._files[row]["index"])
        self._changes[key] = label
        table.update_cell(key, table.ordered_columns[4].key, label)
        if row + 1 < table.row_count:
            table.move_cursor(row=row + 1)

    def action_apply(self) -> None:
        self.dismiss(self._changes or None)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.action_apply()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class HelpModal(TorshModal):
    """Key reference, generated from the app's own bindings."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
        Binding("q,question_mark", "cancel", "Close"),
    ]

    def __init__(self, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        super().__init__()
        self._sections = sections

    def compose(self) -> ComposeResult:
        from rich.table import Table
        from rich.text import Text

        grid = Table.grid(padding=(0, 3))
        grid.add_column(justify="right", style="bold cyan", no_wrap=True)
        grid.add_column()
        for title, rows in self._sections:
            grid.add_row("", Text(title, style="bold magenta"))
            for key, description in rows:
                grid.add_row(key, description)
            grid.add_row("", "")
        grid.add_row("ctrl+p", "command palette — every action, fuzzy-searchable")

        with Vertical(classes="dialog dialog-wide", id="help-dialog"):
            yield Static("Keyboard reference", classes="dialog-title")
            # Scrollable: the reference is taller than a 24-row terminal, and a
            # dialog that simply overflows hides its own Close button.
            with VerticalScroll(id="help-scroll"):
                yield Static(grid, classes="dialog-body")
            with Horizontal(classes="buttons"):
                yield Button("Close", variant="primary", id="cancel")

    @on(Button.Pressed, "#cancel")
    def _close(self) -> None:
        self.dismiss(None)


__all__ = [
    "AddTorrentModal",
    "HelpModal",
    "LimitsModal",
    "MoveModal",
    "PrioritiesModal",
    "RemoveModal",
]
