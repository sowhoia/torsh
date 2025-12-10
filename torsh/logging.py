import logging
import os
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = os.environ.get("TORSH_LOG_LEVEL", "INFO").upper()
    logger.setLevel(level)

    # Write to file instead of stderr to not break TUI
    log_file = Path.home() / ".cache" / "torsh" / "debug.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def setup_file_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("torsh.file")
    if logger.handlers:
        return logger

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handler.setFormatter(logging.Formatter(fmt))

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


