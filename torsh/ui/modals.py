from __future__ import annotations

from typing import Any, TypeVar

from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, SelectionList, Static

from ..logging import get_logger

T = TypeVar("T")
LOG = get_logger(__name__)


class BaseModalScreen(ModalScreen[T]):
    """Base modal screen that dismisses on Escape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddTorrentScreen(BaseModalScreen[tuple[str, str] | None]):
    """Prompt for a magnet link / .torrent path and a destination directory."""

    def __init__(self, download_dir: str) -> None:
        super().__init__()
        self.download_dir = download_dir

    def compose(self):
        with Container(classes="modal-container", id="add-box"):
            yield Static("Add Torrent", classes="modal-title")
            yield Label("Magnet Link or File Path:")
            yield Input(
                placeholder="magnet:?xt=urn:btih:... or /path/to/file.torrent",
                id="link",
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
        LOG.info("AddTorrentScreen submit: link='%s', dir='%s'", link, directory)
        if link:
            self.dismiss((link, directory or self.download_dir))
        else:
            self.dismiss(None)


class ConfirmScreen(BaseModalScreen[bool]):
    """Yes/No confirmation dialog."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("enter", "yes", "Yes"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self):
        with Container(classes="modal-container", id="confirm-box"):
            yield Static("Confirmation", classes="modal-title")
            yield Label(self.message, classes="modal-label")
            with Horizontal(classes="buttons"):
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_mount(self) -> None:
        try:
            self.query_one("#yes", Button).focus()
        except Exception:
            pass

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class MoveScreen(BaseModalScreen[str | None]):
    """Move a torrent's data to a new location."""

    def __init__(self, current_dir: str) -> None:
        super().__init__()
        self.current_dir = current_dir

    def compose(self):
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
    """Set download/upload speed limits (KiB/s)."""

    PRESETS = {
        "preset_off": (0, 0),
        "preset_stream": (8192, 2048),
        "preset_save": (256, 64),
    }

    def __init__(self, down: int, up: int) -> None:
        super().__init__()
        self.down = down
        self.up = up

    def compose(self):
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
    """Choose per-file download priorities (high / normal / low)."""

    def __init__(self, files: dict[int, dict[str, Any]]) -> None:
        super().__init__()
        self.files = files

    def compose(self):
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
        high_selected = set(self.query_one("#high", SelectionList).selected)
        low_selected = set(self.query_one("#low", SelectionList).selected)
        high = [int(v) for v in high_selected]
        low = [int(v) for v in low_selected if v not in high_selected]
        normal = [int(k) for k in self.files.keys() if k not in high and k not in low]
        self.dismiss((high, normal, low))


class FilterScreen(BaseModalScreen[str | None]):
    """Filter the torrent list by name."""

    def __init__(self, current_filter: str) -> None:
        super().__init__()
        self.current_filter = current_filter

    def compose(self):
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
    """Keyboard shortcut reference."""

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

    def compose(self):
        with Container(classes="modal-container", id="help-box"):
            yield Static("⌨️ Keyboard Shortcuts", classes="modal-title")
            yield Markdown(self.HELP_TEXT)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
