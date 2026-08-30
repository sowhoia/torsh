"""Typed TOML configuration.

The file lives at ``~/.config/torsh/config.toml`` and is created on first
run.  Every field can be addressed with a dotted key (``limits.download``)
through :func:`get_key`/:func:`set_key`, which powers ``torsh config``.

Design rules:

* unknown keys in the file are ignored, never fatal;
* values are clamped/coerced instead of raising, so a hand-edited config can
  not brick the app;
* rate limits are stored as bytes/second integers but accept human input
  ("2M", "500K", "off") everywhere.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

from . import atomic, log, paths
from .select import GROUPS, SORT_KEYS  # the one registry of sort keys / status groups
from .units import parse_size

LOG = log.get(__name__)

_MAX_REFRESH_HZ = 10.0


@dataclass
class DownloadsConfig:
    dir: str = ""  # resolved lazily so the default follows $HOME in tests
    sequential: bool = False  # stream-friendly piece order for new torrents
    start_paused: bool = False

    def resolved_dir(self) -> Path:
        raw = self.dir or str(paths.default_download_dir())
        resolved = Path(raw).expanduser()
        if not resolved.is_absolute():
            # The daemon's cwd is meaningless to the user — anchor a relative
            # configured dir to their home instead of "wherever torsh started".
            resolved = Path.home() / resolved
        return resolved


@dataclass
class LimitsConfig:
    download: int = 0  # bytes/s, 0 = unlimited
    upload: int = 0
    ratio: float = 0.0  # pause seeding at this share ratio, 0 = seed forever
    active_downloads: int = 8
    active_seeds: int = 12


@dataclass
class NetworkConfig:
    port: int = 0  # listen port, 0 = pick at random
    dht: bool = True
    upnp: bool = True
    natpmp: bool = True
    lsd: bool = True
    utp: bool = True
    encryption: str = "prefer"  # require | prefer | allow
    max_connections: int = 400


@dataclass
class DaemonConfig:
    autostart: bool = True  # any CLI/TUI command may spawn the daemon
    resume_save_minutes: float = 3.0


@dataclass
class UIConfig:
    refresh_hz: float = 1.0
    show_details: bool = True
    confirm_delete: bool = True
    sort: str = "added"  # column key, "-" prefix for descending
    status_filter: str = "all"


@dataclass
class Config:
    downloads: DownloadsConfig = field(default_factory=DownloadsConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    ui: UIConfig = field(default_factory=UIConfig)


# ---------------------------------------------------------------------------
# Coercion helpers — the config must never crash torsh, whatever is in it.
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_rate(value: Any, default: int) -> int:
    """Rate limits accept human strings in the file too."""
    try:
        return max(0, parse_size(value))
    except (TypeError, ValueError):
        return default


def _coerce_field(name: str, value: Any, default: Any) -> Any:
    if name in {"download", "upload"}:
        return _coerce_rate(value, default)
    if isinstance(default, bool):
        return _coerce_bool(value, default)
    if isinstance(default, int):
        return _coerce_int(value, default)
    if isinstance(default, float):
        return _coerce_float(value, default)
    if isinstance(default, str):
        return str(value) if value is not None else default
    return value if value is not None else default


def _clamp(config: Config) -> Config:
    net, lim, ui = config.network, config.limits, config.ui
    net.port = min(65535, max(0, net.port))
    if net.encryption not in {"require", "prefer", "allow"}:
        net.encryption = "prefer"
    net.max_connections = min(10_000, max(16, net.max_connections))
    lim.active_downloads = min(1000, max(1, lim.active_downloads))
    lim.active_seeds = min(1000, max(1, lim.active_seeds))
    lim.ratio = max(0.0, lim.ratio)
    ui.refresh_hz = min(_MAX_REFRESH_HZ, max(0.2, ui.refresh_hz))
    # A nonsense sort/filter is repaired here rather than in each frontend:
    # the TUI would silently ignore it, and `config list` would keep showing
    # a setting that does nothing.
    if ui.sort.lstrip("-") not in SORT_KEYS:
        ui.sort = UIConfig.sort
    if ui.status_filter not in GROUPS:
        ui.status_filter = UIConfig.status_filter
    config.daemon.resume_save_minutes = min(60.0, max(0.5, config.daemon.resume_save_minutes))
    return config


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def _from_dict(data: dict[str, Any]) -> Config:
    config = Config()
    for section_field in fields(config):
        section = getattr(config, section_field.name)
        raw = data.get(section_field.name)
        if not isinstance(raw, dict):
            continue
        for f in fields(section):
            if f.name in raw:
                default = getattr(section, f.name)
                setattr(section, f.name, _coerce_field(f.name, raw[f.name], default))
    return _clamp(config)


def to_dict(config: Config) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section_field in fields(config):
        section = getattr(config, section_field.name)
        assert is_dataclass(section)
        result[section_field.name] = {f.name: getattr(section, f.name) for f in fields(section)}
    return result


def load(*, env: bool = True) -> Config:
    """Read the config file, falling back to (and writing) defaults.

    ``env=False`` skips the ``TORSH_*`` overrides — required for every
    load-modify-save cycle, otherwise a per-process override would be
    baked into the file permanently.
    """
    file = paths.config_file()
    data: dict[str, Any] = {}
    if file.exists():
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            # Falling back to defaults keeps torsh usable, but doing it in
            # total silence makes a typo look like torsh forgetting settings.
            LOG.warning("ignoring unreadable config %s: %s", file, exc)
            data = {}
    else:
        legacy = _migrate_legacy_yaml()
        if legacy:
            data = legacy
    config = _from_dict(data)
    if not file.exists():
        save(config)  # materialise defaults before env overrides can leak in
    if env:
        _apply_env_overrides(config)
    return config


def parse_error() -> str | None:
    """Why the config file is being ignored, if it is — powers ``torsh doctor``.

    :func:`load` is deliberately forgiving, which means a typo shows up as
    "my settings reverted" rather than an error.  This is how the user gets
    told which line to fix.
    """
    file = paths.config_file()
    if not file.exists():
        return None
    try:
        tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return str(exc)
    return None


def save(config: Config) -> None:
    import tomli_w  # deferred: the CLI read path never needs the writer

    # Atomic: `torsh config set`, the TUI persisting its sort order and the
    # daemon persisting speed limits can all land at the same moment.
    atomic.write_text(paths.config_file(), tomli_w.dumps(to_dict(_clamp(config))))


def _apply_env_overrides(config: Config) -> None:
    """A few high-value env knobs; everything else goes through the file."""
    env_dir = os.environ.get("TORSH_DOWNLOAD_DIR")
    if env_dir:
        config.downloads.dir = env_dir
    port = os.environ.get("TORSH_PORT")
    if port:
        config.network.port = _coerce_int(port, config.network.port)


def _migrate_legacy_yaml() -> dict[str, Any] | None:
    """Best-effort import of the single useful key from the 0.x YAML config.

    Pre-1.0 torsh drove an external Transmission daemon; none of those
    settings apply any more except where downloads land.  Parsed with a
    regex on purpose — not worth a YAML dependency.
    """
    legacy = paths.legacy_config_file()
    if not legacy.exists():
        return None
    try:
        text = legacy.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*download_dir:\s*(.+?)\s*$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip("'\"")
    if not value:
        return None
    return {"downloads": {"dir": value}}


# ---------------------------------------------------------------------------
# Dotted-key access for `torsh config get/set`
# ---------------------------------------------------------------------------


def list_keys(config: Config) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    for section, values in to_dict(config).items():
        for key, value in values.items():
            pairs.append((f"{section}.{key}", value))
    return pairs


def get_key(config: Config, dotted: str) -> Any:
    section_name, _, key = dotted.partition(".")
    if not key:
        raise KeyError(dotted)
    try:
        section = getattr(config, section_name)
        if not is_dataclass(section):
            raise AttributeError
        return getattr(section, key)
    except AttributeError:
        raise KeyError(dotted) from None


def set_key(config: Config, dotted: str, raw: str) -> Any:
    """Set a dotted key from string input, returning the coerced value.

    Unlike file loading (which is forgiving), explicit ``config set`` input
    raises :class:`ValueError` on garbage so the user sees what went wrong.
    """
    section_name, _, key = dotted.partition(".")
    current = get_key(config, dotted)  # raises KeyError for unknown keys
    section = getattr(config, section_name)

    value: Any
    if key in {"download", "upload"}:
        value = parse_size(raw)  # raises ValueError with a helpful message
    elif isinstance(current, bool):
        cleaned = raw.strip().lower()
        if cleaned not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise ValueError(f"{dotted} expects true/false, got {raw!r}")
        value = cleaned in {"1", "true", "yes", "on"}
    elif isinstance(current, int):
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{dotted} expects an integer, got {raw!r}") from None
    elif isinstance(current, float):
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{dotted} expects a number, got {raw!r}") from None
    else:
        value = raw

    setattr(section, key, value)
    _clamp(config)
    return getattr(section, key)
