import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


CONFIG_DIR = Path(os.environ.get("TORSH_CONFIG_DIR", "~/.config/torsh")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.yaml"


@dataclass
class RpcConfig:
    host: str = os.environ.get("TORSH_HOST", "localhost")
    port: int = int(os.environ.get("TORSH_PORT", "9091"))
    username: str | None = os.environ.get("TORSH_USER") or None
    password: str | None = os.environ.get("TORSH_PASSWORD") or None
    timeout: float = float(os.environ.get("TORSH_TIMEOUT", "10.0"))


@dataclass
class DaemonConfig:
    autostart: bool = os.environ.get("TORSH_AUTOSTART", "true").lower() != "false"
    binary: str = os.environ.get("TORSH_DAEMON", "transmission-daemon")
    extra_args: list[str] = field(default_factory=list)
    install_missing: bool = os.environ.get("TORSH_INSTALL_MISSING", "true").lower() != "false"
    restart_on_fail: bool = os.environ.get("TORSH_RESTART_ON_FAIL", "true").lower() != "false"
    log_path: Path = Path(os.environ.get("TORSH_LOG", "") or (CONFIG_DIR / "daemon.log")).expanduser()


@dataclass
class PathConfig:
    download_dir: Path = Path(os.environ.get("TORSH_DOWNLOAD_DIR", "~/Downloads/torrents")).expanduser()
    config_dir: Path = CONFIG_DIR


@dataclass
class UIConfig:
    refresh_interval: float = 2.5
    sort_column: int | None = None
    sort_desc: bool = False
    filter_text: str = ""
    status_filter: str = "any"
    progress_filter: str = "any"


@dataclass
class AppConfig:
    rpc: RpcConfig = field(default_factory=RpcConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    ui: UIConfig = field(default_factory=UIConfig)


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> AppConfig:
    ensure_config_dir()
    if CONFIG_FILE.exists():
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    else:
        data = {}

    rpc_data: Dict[str, Any] = data.get("rpc", {})
    daemon_data: Dict[str, Any] = data.get("daemon", {})
    paths_data: Dict[str, Any] = data.get("paths", {})
    ui_data: Dict[str, Any] = data.get("ui", {})

    config = AppConfig(
        rpc=RpcConfig(
            host=rpc_data.get("host", RpcConfig().host),
            port=int(rpc_data.get("port", RpcConfig().port)),
            username=rpc_data.get("username", RpcConfig().username),
            password=rpc_data.get("password", RpcConfig().password),
            timeout=float(rpc_data.get("timeout", RpcConfig().timeout)),
        ),
        daemon=DaemonConfig(
            autostart=bool(daemon_data.get("autostart", DaemonConfig().autostart)),
            binary=daemon_data.get("binary", DaemonConfig().binary),
            extra_args=daemon_data.get("extra_args", DaemonConfig().extra_args),
            install_missing=bool(daemon_data.get("install_missing", DaemonConfig().install_missing)),
            restart_on_fail=bool(daemon_data.get("restart_on_fail", DaemonConfig().restart_on_fail)),
            log_path=Path(daemon_data.get("log_path", DaemonConfig().log_path)).expanduser(),
        ),
        paths=PathConfig(
            download_dir=Path(paths_data.get("download_dir", PathConfig().download_dir)).expanduser(),
            config_dir=Path(paths_data.get("config_dir", PathConfig().config_dir)).expanduser(),
        ),
        ui=UIConfig(
            refresh_interval=float(ui_data.get("refresh_interval", UIConfig().refresh_interval)),
            sort_column=ui_data.get("sort_column", UIConfig().sort_column),
            sort_desc=bool(ui_data.get("sort_desc", UIConfig().sort_desc)),
            filter_text=ui_data.get("filter_text", UIConfig().filter_text),
            status_filter=ui_data.get("status_filter", UIConfig().status_filter),
            progress_filter=ui_data.get("progress_filter", UIConfig().progress_filter),
        ),
    )

    save_config(config)
    return config


def save_config(config: AppConfig) -> None:
    ensure_config_dir()
    payload = {
        "rpc": {
            "host": config.rpc.host,
            "port": config.rpc.port,
            "username": config.rpc.username or "",
            "password": config.rpc.password or "",
            "timeout": config.rpc.timeout,
        },
        "daemon": {
            "autostart": config.daemon.autostart,
            "binary": config.daemon.binary,
            "extra_args": config.daemon.extra_args,
            "install_missing": config.daemon.install_missing,
            "restart_on_fail": config.daemon.restart_on_fail,
            "log_path": str(config.daemon.log_path),
        },
        "paths": {
            "download_dir": str(config.paths.download_dir),
            "config_dir": str(config.paths.config_dir),
        },
        "ui": {
            "refresh_interval": config.ui.refresh_interval,
            "sort_column": config.ui.sort_column,
            "sort_desc": config.ui.sort_desc,
            "filter_text": config.ui.filter_text,
            "status_filter": config.ui.status_filter,
            "progress_filter": config.ui.progress_filter,
        },
    }
    CONFIG_FILE.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))


