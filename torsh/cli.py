"""Command-line entry point for torsh.

Renders a polished boot sequence (banner + live status), ensures the
Transmission daemon is reachable, then hands control to the Textual TUI.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .config import AppConfig, load_config, save_config
from .daemon import maybe_start_daemon, rpc_reachable
from .logging import get_logger
from .ui import console as boot
from .ui.app import TorshApp

LOG = get_logger(__name__)


def _apply_overrides(
    config: AppConfig,
    *,
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    download_dir: str | None,
) -> AppConfig:
    """Apply CLI overrides onto the loaded config and persist if changed."""
    if host:
        config.rpc.host = host
    if port is not None:
        config.rpc.port = port
    if user is not None:
        config.rpc.username = user
    if password is not None:
        config.rpc.password = password
    if download_dir:
        config.paths.download_dir = Path(download_dir).expanduser()
    save_config(config)
    return config


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--host", default=None, help="Transmission RPC host (default: localhost)")
@click.option("--port", default=None, type=int, help="RPC port (default: 9091)")
@click.option("--user", default=None, help="RPC username")
@click.option("--password", default=None, help="RPC password")
@click.option("--download-dir", default=None, help="Default download directory")
@click.option("--no-autostart", is_flag=True, help="Do not auto-start transmission-daemon")
@click.option("--no-install-missing", is_flag=True, help="Skip auto-install of transmission")
@click.option("--no-banner", is_flag=True, help="Skip the startup banner")
@click.version_option(__version__, "-v", "--version", message="torsh %(version)s")
def main(
    host: str | None,
    port: int | None,
    user: str | None,
    password: str | None,
    download_dir: str | None,
    no_autostart: bool,
    no_install_missing: bool,
    no_banner: bool,
) -> None:
    """Launch the torsh TUI."""
    console = Console()

    config = load_config()
    config = _apply_overrides(
        config,
        host=host,
        port=port,
        user=user,
        password=password,
        download_dir=download_dir,
    )
    if no_autostart:
        config.daemon.autostart = False
    if no_install_missing:
        config.daemon.install_missing = False

    if not no_banner:
        boot.print_banner(console)

    _boot_daemon(console, config)

    app = TorshApp(config=config)
    try:
        asyncio.run(app.run_async())
    except KeyboardInterrupt:
        LOG.info("Interrupted by user (Ctrl+C)")
    finally:
        console.print("\n[#6b7394]torsh out. Happy seeding.[/]\n")


def _boot_daemon(console: Console, config: AppConfig) -> None:
    """Run the visible pre-flight: prepare the daemon and probe the RPC port."""
    host, port = config.rpc.host, config.rpc.port

    if config.daemon.autostart and not rpc_reachable(host, port):
        try:
            with boot.boot_step(console, "Starting transmission-daemon"):
                maybe_start_daemon(config)
                # config.rpc.port may have changed if the port was busy.
                host, port = config.rpc.host, config.rpc.port
        except Exception as exc:  # pragma: no cover - defensive
            LOG.error("Daemon startup failed: %s", exc)

    with console.status(f"[#b7c4ff]Connecting to {host}:{port}", spinner="dots", spinner_style="#00f0ff"):
        connected = rpc_reachable(host, port)

    if connected:
        boot.print_ready(console, host, port)
    else:
        boot.print_offline_hint(console, host, port)
    console.print()


if __name__ == "__main__":
    main()
