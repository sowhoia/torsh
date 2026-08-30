"""Human-friendly units: sizes, rates, durations, ratios.

One tiny, dependency-free module used by both the CLI and the TUI so every
surface formats numbers identically.  Parsing accepts what people actually
type (``2M``, ``1.5 GiB``, ``500k``, ``off``); formatting favours short,
aligned output that reads well in tables.
"""

from __future__ import annotations

import re

_BINARY_STEPS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]

# Suffix → multiplier for parsing. Users rarely mean decimal kilobytes when
# rate-limiting torrents, so bare "k"/"m"/"g" are binary here.
_SUFFIXES: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-z]*)\s*$", re.IGNORECASE)

_UNLIMITED_WORDS = {"off", "none", "unlimited", "inf", "0"}


def fmt_size(n: float | int | None, *, precision: int = 1) -> str:
    """``1536`` → ``"1.5 KiB"``.  ``None`` → ``"—"``."""
    if n is None:
        return "—"
    value = float(max(0, n))
    for unit in _BINARY_STEPS:
        if value < 1024.0 or unit == _BINARY_STEPS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.{precision}f} PiB"  # pragma: no cover - unreachable


def fmt_rate(n: float | int | None) -> str:
    """Bytes/second → ``"1.5 MiB/s"``; zero and ``None`` → ``"—"``."""
    if not n or n <= 0:
        return "—"
    return f"{fmt_size(n)}/s"


def fmt_limit(n: int | None) -> str:
    """A configured rate limit: ``0``/``None`` mean unlimited → ``"∞"``."""
    if not n or n <= 0:
        return "∞"
    return fmt_rate(n)


def parse_size(text: str | int | float | None) -> int:
    """Parse ``"2M"``, ``"1.5 GiB"``, ``"500k"``, ``512``, ``"off"`` → bytes.

    ``off``/``none``/``unlimited``/``0`` → 0 (torsh's "no limit" sentinel).
    Raises :class:`ValueError` for garbage so callers can show a clean error.
    """
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return max(0, int(text))
    cleaned = text.strip().lower().removesuffix("/s")  # tolerate "2M/s"
    if cleaned in _UNLIMITED_WORDS:
        return 0
    match = _SIZE_RE.match(cleaned)
    if not match:
        raise ValueError(f"can't parse size {text!r} (try 500K, 2M, 1.5G or 'off')")
    number, suffix = match.groups()
    multiplier = _SUFFIXES.get(suffix)
    if multiplier is None:
        raise ValueError(f"unknown unit {suffix!r} in {text!r} (try K, M, G)")
    return int(float(number) * multiplier)


def fmt_duration(seconds: float | int | None, *, compact: bool = True) -> str:
    """Seconds → ``"4m 12s"`` / ``"2h 05m"`` / ``"3d 4h"``.

    Negative or ``None`` → ``"—"`` (unknown); > 30 days → ``"∞"`` — a torrent
    ETA that long is noise, not information.
    """
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds == 0:
        return "0s"
    if seconds > 30 * 86400:
        return "∞"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" if compact else f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def fmt_eta(seconds: float | int | None) -> str:
    """ETA formatting: 0 means "done", unknown means "—"."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds == 0:
        return "✓"
    return fmt_duration(seconds)


def fmt_ratio(ratio: float | None) -> str:
    if ratio is None or ratio < 0:
        return "—"
    if ratio >= 100:
        return f"{ratio:.0f}"
    return f"{ratio:.2f}"


def fmt_percent(fraction: float | None) -> str:
    """0..1 fraction → ``"42.7%"`` with a stable width."""
    if fraction is None:
        return "    —"
    return f"{min(1.0, max(0.0, fraction)) * 100:5.1f}%"
