import os
import shutil
import subprocess
import time
import socket
import json
from pathlib import Path
from typing import Iterable

from .config import AppConfig, save_config
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


def _has_flag(args: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(f"{flag}=") for a in args)


def _pick_free_port(start: int, attempts: int = 10) -> int:
    port = start
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    return start


def _write_settings_ports(cfg_dir: Path, rpc_port: int, peer_port: int | None) -> None:
    settings_path = cfg_dir / "settings.json"
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text() or "{}")
        except Exception:
            data = {}
    # Apply ports
    data["rpc-port"] = rpc_port
    if peer_port:
        data["peer-port"] = peer_port
        data["peer-port-random-on-start"] = False
    settings_path.write_text(json.dumps(data, indent=2))


def _build_daemon_args(config: AppConfig, peer_port: int | None) -> list[str]:
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
    extra = config.daemon.extra_args or []
    if peer_port and not _has_flag(extra, "--peerport"):
        args.extend(["--peerport", str(peer_port)])
    args.extend(extra)
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

    # Pick ports if defaults are occupied
    chosen_rpc_port = _pick_free_port(config.rpc.port)
    if chosen_rpc_port != config.rpc.port:
        LOG.warning("RPC port %s busy, switching to %s", config.rpc.port, chosen_rpc_port)
        config.rpc.port = chosen_rpc_port
        save_config(config)

    chosen_peer_port = _pick_free_port(51413)
    if chosen_peer_port != 51413:
        LOG.warning("Peer port 51413 busy, switching to %s", chosen_peer_port)

    # Write ports into settings.json so daemon picks them up (rpc-port/peer-port)
    _write_settings_ports(config.paths.config_dir, chosen_rpc_port, chosen_peer_port)

    args = _build_daemon_args(config, chosen_peer_port)
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


