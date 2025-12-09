import asyncio
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .config import AppConfig, load_config, save_config
from .daemon import maybe_start_daemon
from .logging import get_logger
from .ui.app import TorshApp


LOG = get_logger(__name__)


def _apply_overrides(config: AppConfig, host: str, port: int, user: str | None, password: str | None, download_dir: str | None):
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
@click.version_option(__version__, "-v", "--version", message="torsh %(version)s")
def main(host: Optional[str], port: Optional[int], user: Optional[str], password: Optional[str], download_dir: Optional[str], no_autostart: bool, no_install_missing: bool):
    """Run torsh TUI."""
    config = load_config()
    config = _apply_overrides(config, host, port, user, password, download_dir)
    if no_autostart:
        config.daemon.autostart = False
    if no_install_missing:
        config.daemon.install_missing = False

    maybe_start_daemon(config)

    app = TorshApp(config=config)
    try:
        asyncio.run(app.run_async())
    except KeyboardInterrupt:
        LOG.info("Interrupted by user (Ctrl+C)")


if __name__ == "__main__":
    main()


