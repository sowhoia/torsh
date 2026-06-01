"""Pre-flight terminal experience for the ``torsh`` command.

Everything in this module runs *before* the Textual TUI takes over the screen:
a wordmark banner and a live boot sequence that ensures the daemon is up and
the RPC endpoint is reachable, with friendly diagnostics when it is not.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from .. import __version__

# A compact block-font wordmark; rendered with a cyan→magenta gradient.
_WORDMARK = r"""
▗▄▄▄▖▗▄▄▖  ▗▄▄▖▗▄▄▖▗▖ ▗▖
  █ ▐▌ ▐▌▐▌   ▐▌   ▐▌ ▐▌
  █ ▐▌ ▐▌▐▌▝▜▌ ▝▀▚▖▐▛▀▜▌
  █ ▝▚▄▞▘▝▚▄▟▌▗▄▄▞▘▐▌ ▐▌
""".strip("\n")

# Gradient stops walked across each glyph line (cyan → violet → magenta).
_GRADIENT = ["#00f0ff", "#33d0ff", "#8f7fff", "#c75fff", "#ff3cac"]


def _gradient_line(line: str, width: int) -> Text:
    """Colour a glyph line with a smooth left→right gradient."""
    text = Text()
    span = max(1, width - 1)
    for col, char in enumerate(line):
        idx = round((col / span) * (len(_GRADIENT) - 1))
        text.append(char, style=_GRADIENT[idx])
    return text


def banner() -> Panel:
    """Build the boot banner panel (gradient wordmark + tagline)."""
    lines = _WORDMARK.splitlines()
    width = max(len(line) for line in lines)
    lines = [line.ljust(width) for line in lines]
    wordmark = Text("\n").join(_gradient_line(line, width) for line in lines)
    tagline = Text("a beautiful Transmission client for your terminal", style="italic #6b7394")
    version = Text(f"v{__version__}", style="bold #2effa8")

    body = Group(
        Align.center(wordmark),
        Align.center(Text()),
        Align.center(tagline),
    )
    return Panel(
        body,
        border_style="#24304a",
        title=version,
        title_align="right",
        padding=(1, 4),
    )


def print_banner(console: Console) -> None:
    console.print()
    console.print(banner())
    console.print()


def _hint_panel(host: str, port: int) -> Panel:
    tips = Text()
    tips.append("Could not reach the Transmission daemon.\n\n", style="bold #ff5f6d")
    tips.append("Try one of the following:\n", style="#b7c4ff")
    for label in (
        f"start it manually:  transmission-daemon --port {port}",
        "install it:         torsh runs apt/brew/dnf for you when missing",
        f"point torsh at it:  torsh --host {host} --port {port}",
    ):
        tips.append("  • ", style="#00f0ff")
        tips.append(f"{label}\n", style="#6b7394")
    tips.append("\nLaunching anyway — torsh will keep retrying in the background.", style="italic #ffcf6f")
    return Panel(tips, border_style="#ff5f6d", title="⚠ offline", title_align="left", padding=(1, 2))


@contextmanager
def boot_step(console: Console, message: str) -> Iterator[None]:
    """Show a live spinner for a single boot step, marking ✓/✗ on exit."""
    with console.status(Text(message, style="#b7c4ff"), spinner="dots", spinner_style="#00f0ff"):
        try:
            yield
        except Exception:
            console.print(Text("  ✗ ", style="bold #ff5f6d") + Text(message, style="#6b7394"))
            raise
    console.print(Text("  ✓ ", style="bold #2effa8") + Text(message, style="#6b7394"))


def print_offline_hint(console: Console, host: str, port: int) -> None:
    console.print(_hint_panel(host, port))


def print_ready(console: Console, host: str, port: int) -> None:
    msg = Text("  ● ", style="bold #2effa8")
    msg.append("Connected to ", style="#6b7394")
    msg.append(f"{host}:{port}", style="bold #00f0ff")
    msg.append("  —  press ", style="#6b7394")
    msg.append("?", style="bold #ff3cac")
    msg.append(" for help", style="#6b7394")
    console.print(msg)
