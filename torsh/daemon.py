import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from .config import AppConfig
from .logging import get_logger


LOG = get_logger(__name__)


def _is_daemon_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "transmission-daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _detect_package_manager() -> str | None:
    managers = ["apt-get", "apt", "brew", "dnf", "yum", "pacman", "zypper"]
    for mgr in managers:
        if shutil.which(mgr):
            return mgr
    return None


def _install_transmission(manager: str) -> bool:
    commands = {
        "apt-get": ["sudo", "apt-get", "update", "&&", "sudo", "apt-get", "-y", "install", "transmission-daemon"],
        "apt": ["sudo", "apt", "update", "&&", "sudo", "apt", "-y", "install", "transmission-daemon"],
        "brew": ["brew", "install", "transmission"],
        "dnf": ["sudo", "dnf", "-y", "install", "transmission-daemon"],
        "yum": ["sudo", "yum", "-y", "install", "transmission-daemon"],
        "pacman": ["sudo", "pacman", "-Sy", "--noconfirm", "transmission-cli"],
        "zypper": ["sudo", "zypper", "--non-interactive", "install", "transmission-daemon"],
    }
    cmd = commands.get(manager)
    if not cmd:
        return False
    LOG.info("Trying to install transmission via %s", manager)
    try:
        # emulate '&&' without shell
        if "&&" in cmd:
            # apt/apt-get branch
            update = cmd[:3]
            install = cmd[4:]
            subprocess.run(update, check=False)
            result = subprocess.run(install, check=False)
        else:
            result = subprocess.run(cmd, check=False)
        return result.returncode == 0
    except Exception as exc:  # pragma: no cover - safeguard
        LOG.error("Auto-install failed: %s", exc)
        return False


def ensure_transmission_available(config: AppConfig) -> bool:
    """Ensure transmission binary exists; optionally try to install it."""
    if shutil.which(config.daemon.binary):
        return True
    if not config.daemon.install_missing:
        LOG.warning("Binary %s not found, auto-install is disabled", config.daemon.binary)
        return False

    mgr = _detect_package_manager()
    if not mgr:
        LOG.warning("No supported package manager found for auto-install")
        return False

    ok = _install_transmission(mgr)
    if not ok:
        LOG.warning("Auto-install via %s failed", mgr)
        return False

    return shutil.which(config.daemon.binary) is not None


def _build_daemon_args(config: AppConfig) -> list[str]:
    cfg_dir = config.paths.config_dir
    download_dir = config.paths.download_dir
    cfg_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    args = [
        config.daemon.binary,
        "--foreground",
        "--config-dir",
        str(cfg_dir),
        "--download-dir",
        str(download_dir),
        "--log-info",
    ]
    args.extend(config.daemon.extra_args or [])
    return args


def maybe_start_daemon(config: AppConfig, wait_seconds: float = 2.5) -> None:
    if not config.daemon.autostart:
        LOG.info("Daemon autostart disabled")
        return

    if not ensure_transmission_available(config):
        LOG.error("Transmission unavailable. Install manually or enable auto-install.")
        return

    if _is_daemon_running():
        LOG.debug("transmission-daemon is already running")
        return

    binary = shutil.which(config.daemon.binary)
    if not binary:
        LOG.warning("Binary transmission-daemon not found: %s", config.daemon.binary)
        return

    args = _build_daemon_args(config)
    log_file = config.daemon.log_path
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log = log_file.open("a", encoding="utf-8")

    LOG.info("Starting transmission-daemon: %s", " ".join(args))
    try:
        subprocess.Popen(
            args,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:  # pragma: no cover - safeguard
        LOG.error("Failed to start transmission-daemon: %s", exc)
        return

    # give daemon time to start
    time.sleep(wait_seconds)


def stop_daemon(process_names: Iterable[str] = ("transmission-daemon",)) -> None:
    for name in process_names:
        try:
            subprocess.run(["pkill", "-x", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            LOG.debug("pkill is not available on this system")
            break

    # small delay to let daemon exit
    time.sleep(0.5)


