from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional


DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_LEVEL = os.environ.get("TORSH_LOG_LEVEL", "INFO").upper()
DEFAULT_LOG_PATH = Path(os.environ.get("TORSH_LOG_FILE", "") or (Path.home() / ".cache" / "torsh" / "debug.log"))


def _build_handler(to_stdout: bool, path: Optional[Path]) -> logging.Handler:
    if to_stdout:
        handler = logging.StreamHandler()
    else:
        target = path or DEFAULT_LOG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
    return handler


def configure_logger(
    name: str,
    *,
    level: str | int | None = None,
    to_stdout: bool | None = None,
    path: Optional[Path] = None,
) -> logging.Logger:
    """Create or reuse a logger with consistent handlers."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    resolved_level = level or DEFAULT_LEVEL
    resolved_stdout = to_stdout if to_stdout is not None else _env_bool("TORSH_LOG_TO_STDOUT", False)

    logger.setLevel(resolved_level)
    handler = _build_handler(resolved_stdout, path)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """Backward-compatible entry point for the app."""
    return configure_logger(name)


def setup_file_logger(path: Path) -> logging.Logger:
    return configure_logger("torsh.file", to_stdout=False, path=path, level=DEFAULT_LEVEL)


def _env_bool(key: str, default: bool) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


