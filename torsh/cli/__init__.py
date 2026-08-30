"""torsh command-line interface.

``torsh`` with no arguments opens the TUI; everything else is a scriptable
subcommand in the spirit of ``gh``/``docker``: humane selectors, ``--json``
everywhere it matters, clean exit codes (0 ok, 1 failure, 2 usage).
"""

from __future__ import annotations

import rich_click as click

from .. import __version__
from . import render, system, torrents

# ---------------------------------------------------------------------------
# rich-click appearance
# ---------------------------------------------------------------------------

if hasattr(click.rich_click, "TEXT_MARKUP"):  # rich-click ≥ 1.9
    click.rich_click.TEXT_MARKUP = "rich"
else:  # pragma: no cover - older rich-click
    click.rich_click.USE_RICH_MARKUP = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = False
click.rich_click.STYLE_OPTION = f"bold {render.CYAN}"
click.rich_click.STYLE_ARGUMENT = f"bold {render.MAGENTA}"
click.rich_click.STYLE_COMMAND = f"bold {render.CYAN}"
click.rich_click.STYLE_HELPTEXT_FIRST_LINE = "bold"
click.rich_click.STYLE_HELPTEXT = ""
click.rich_click.STYLE_USAGE = f"bold {render.VIOLET}"
click.rich_click.MAX_WIDTH = 100
click.rich_click.COMMAND_GROUPS = {
    "torsh": [
        {
            "name": "Torrents",
            "commands": ["add", "get", "ls", "info", "pause", "resume", "rm", "files", "peers", "trackers"],
        },
        {
            "name": "Tuning",
            "commands": [
                "speed",
                "limit",
                "seq",
                "prio",
                "queue",
                "move",
                "export",
                "verify",
                "reannounce",
                "stats",
            ],
        },
        {
            "name": "System",
            "commands": ["daemon", "config", "doctor", "completion"],
        },
    ]
}


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="torsh")
@click.pass_context
def main(ctx: click.Context) -> None:
    """[bold]torsh[/] — a self-contained BitTorrent client for your terminal.

    Run with no arguments to open the interactive dashboard.
    The engine lives in a background daemon, so downloads keep going
    after you close the UI ([bold]torsh daemon stop[/] to halt everything).

    \b
    Quick start:
      torsh add magnet:…        add and start a download
      torsh                     watch everything in the TUI
      torsh get file.torrent    download one thing, then exit
      torsh ls --json | jq      script against your torrents
    """
    if ctx.invoked_subcommand is None:
        from ..tui.app import run_tui

        run_tui()


# Torrents
main.add_command(torrents.add)
main.add_command(torrents.get)
main.add_command(torrents.ls)
main.add_command(torrents.info)
main.add_command(torrents.pause)
main.add_command(torrents.resume)
main.add_command(torrents.rm)
main.add_command(torrents.files)
main.add_command(torrents.peers)
main.add_command(torrents.trackers)

# Tuning
main.add_command(torrents.speed)
main.add_command(torrents.limit)
main.add_command(torrents.seq)
main.add_command(torrents.prio)
main.add_command(torrents.queue)
main.add_command(torrents.move)
main.add_command(torrents.export)
main.add_command(torrents.verify)
main.add_command(torrents.reannounce)
main.add_command(torrents.stats)

# System
main.add_command(system.daemon)
main.add_command(system.config_group)
main.add_command(system.doctor)
main.add_command(system.completion)
