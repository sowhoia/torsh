from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


CONFIG_DIR = Path(os.environ.get("TORSH_CONFIG_DIR", "~/.config/torsh")).expanduser()
CONFIG_FILE = CONFIG_DIR / "config.yaml"


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value) if value is not None else default


def _safe_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_path(value: Any, default: Path) -> Path:
    if value is None:
        return default.expanduser()
    try:
        return Path(value).expanduser()
    except Exception:
        return default.expanduser()


@dataclass
class RpcConfig:
    host: str = os.environ.get("TORSH_HOST", "localhost")
    port: int = field(default_factory=lambda: _safe_int(os.environ.get("TORSH_PORT", "9091"), 9091, minimum=1, maximum=65535))
    username: str | None = os.environ.get("TORSH_USER") or None
    password: str | None = os.environ.get("TORSH_PASSWORD") or None
    timeout: float = field(default_factory=lambda: _safe_float(os.environ.get("TORSH_TIMEOUT", "10.0"), 10.0))

    def normalize(self) -> "RpcConfig":
        return RpcConfig(
            host=self.host.strip() or "localhost",
            port=_safe_int(self.port, 9091, minimum=1, maximum=65535),
            username=self.username or None,
            password=self.password or None,
            timeout=max(1.0, _safe_float(self.timeout, 10.0)),
        )


@dataclass
class DaemonConfig:
    autostart: bool = os.environ.get("TORSH_AUTOSTART", "true").lower() != "false"
    binary: str = os.environ.get("TORSH_DAEMON", "transmission-daemon")
    extra_args: list[str] = field(default_factory=list)
    install_missing: bool = os.environ.get("TORSH_INSTALL_MISSING", "true").lower() != "false"
    restart_on_fail: bool = os.environ.get("TORSH_RESTART_ON_FAIL", "true").lower() != "false"
    log_path: Path = Path(os.environ.get("TORSH_LOG", "") or (CONFIG_DIR / "daemon.log")).expanduser()

    def normalize(self, config_dir: Path) -> "DaemonConfig":
        log_path = self.log_path or (config_dir / "daemon.log")
        return DaemonConfig(
            autostart=_safe_bool(self.autostart, True),
            binary=self.binary.strip() or "transmission-daemon",
            extra_args=[str(arg).strip() for arg in (self.extra_args or []) if str(arg).strip()],
            install_missing=_safe_bool(self.install_missing, True),
            restart_on_fail=_safe_bool(self.restart_on_fail, True),
            log_path=_safe_path(log_path, config_dir / "daemon.log"),
        )


@dataclass
class PathConfig:
    download_dir: Path = Path(os.environ.get("TORSH_DOWNLOAD_DIR", "~/Downloads/torrents")).expanduser()
    config_dir: Path = CONFIG_DIR

    def normalize(self) -> "PathConfig":
        return PathConfig(
            download_dir=_safe_path(self.download_dir, Path("~/Downloads/torrents")),
            config_dir=_safe_path(self.config_dir, CONFIG_DIR),
        )


@dataclass
class UIConfig:
    refresh_interval: float = 2.5
    sort_column: int | None = None
    sort_desc: bool = False
    filter_text: str = ""
    status_filter: str = "any"
    progress_filter: str = "any"

    def normalize(self) -> "UIConfig":
        return UIConfig(
            refresh_interval=max(0.5, min(30.0, _safe_float(self.refresh_interval, 2.5))),
            sort_column=self.sort_column if self.sort_column in (None, 1, 2, 3, 4, 5, 6, 7, 8) else None,
            sort_desc=_safe_bool(self.sort_desc, False),
            filter_text=self.filter_text or "",
            status_filter=self.status_filter or "any",
            progress_filter=self.progress_filter or "any",
        )


@dataclass
class AppConfig:
    rpc: RpcConfig = field(default_factory=RpcConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    def normalize(self) -> "AppConfig":
        paths = self.paths.normalize()
        return AppConfig(
            rpc=self.rpc.normalize(),
            daemon=self.daemon.normalize(paths.config_dir),
            paths=paths,
            ui=self.ui.normalize(),
        )


def ensure_config_dir(path: Path = CONFIG_DIR) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        # Corrupted YAML should not crash the app; fall back to defaults.
        return {}


def _to_payload(config: AppConfig) -> Dict[str, Any]:
    return {
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


def _merge_config(data: Dict[str, Any]) -> AppConfig:
    defaults = AppConfig()
    rpc_data: Dict[str, Any] = data.get("rpc", {})
    daemon_data: Dict[str, Any] = data.get("daemon", {})
    paths_data: Dict[str, Any] = data.get("paths", {})
    ui_data: Dict[str, Any] = data.get("ui", {})

    cfg = AppConfig(
        rpc=RpcConfig(
            host=rpc_data.get("host", defaults.rpc.host),
            port=_safe_int(rpc_data.get("port", defaults.rpc.port), defaults.rpc.port, minimum=1, maximum=65535),
            username=rpc_data.get("username", defaults.rpc.username) or None,
            password=rpc_data.get("password", defaults.rpc.password) or None,
            timeout=_safe_float(rpc_data.get("timeout", defaults.rpc.timeout), defaults.rpc.timeout),
        ),
        daemon=DaemonConfig(
            autostart=_safe_bool(daemon_data.get("autostart", defaults.daemon.autostart), defaults.daemon.autostart),
            binary=daemon_data.get("binary", defaults.daemon.binary),
            extra_args=daemon_data.get("extra_args", defaults.daemon.extra_args) or [],
            install_missing=_safe_bool(
                daemon_data.get("install_missing", defaults.daemon.install_missing),
                defaults.daemon.install_missing,
            ),
            restart_on_fail=_safe_bool(
                daemon_data.get("restart_on_fail", defaults.daemon.restart_on_fail),
                defaults.daemon.restart_on_fail,
            ),
            log_path=_safe_path(daemon_data.get("log_path", defaults.daemon.log_path), defaults.daemon.log_path),
        ),
        paths=PathConfig(
            download_dir=_safe_path(paths_data.get("download_dir", defaults.paths.download_dir), defaults.paths.download_dir),
            config_dir=_safe_path(paths_data.get("config_dir", defaults.paths.config_dir), defaults.paths.config_dir),
        ),
        ui=UIConfig(
            refresh_interval=_safe_float(ui_data.get("refresh_interval", defaults.ui.refresh_interval), defaults.ui.refresh_interval),
            sort_column=ui_data.get("sort_column", defaults.ui.sort_column),
            sort_desc=_safe_bool(ui_data.get("sort_desc", defaults.ui.sort_desc), defaults.ui.sort_desc),
            filter_text=ui_data.get("filter_text", defaults.ui.filter_text) or "",
            status_filter=ui_data.get("status_filter", defaults.ui.status_filter) or "any",
            progress_filter=ui_data.get("progress_filter", defaults.ui.progress_filter) or "any",
        ),
    )
    return cfg.normalize()


def load_config() -> AppConfig:
    ensure_config_dir(CONFIG_DIR)
    data = _load_yaml(CONFIG_FILE)
    config = _merge_config(data)
    save_config(config)
    return config


def save_config(config: AppConfig) -> None:
    payload = _to_payload(config.normalize())
    target = CONFIG_FILE
    ensure_config_dir(target.parent)

    current = _load_yaml(target)
    if current == payload:
        return

    tmp = target.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))
    tmp.replace(target)


