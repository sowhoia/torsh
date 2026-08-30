"""Units: the one formatting vocabulary shared by CLI and TUI."""

from __future__ import annotations

import pytest

from torsh.units import (
    fmt_duration,
    fmt_eta,
    fmt_limit,
    fmt_percent,
    fmt_rate,
    fmt_ratio,
    fmt_size,
    parse_size,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 B"),
        (512, "512 B"),
        (1536, "1.5 KiB"),
        (1048576, "1.0 MiB"),
        (3 * 1024**3, "3.0 GiB"),
        (None, "—"),
    ],
)
def test_fmt_size(value, expected):
    assert fmt_size(value) == expected


def test_fmt_rate_zero_is_quiet():
    assert fmt_rate(0) == "—"
    assert fmt_rate(None) == "—"
    assert fmt_rate(1536) == "1.5 KiB/s"


def test_fmt_limit_unlimited():
    assert fmt_limit(0) == "∞"
    assert fmt_limit(None) == "∞"
    assert fmt_limit(2 * 1024**2) == "2.0 MiB/s"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2M", 2 * 1024**2),
        ("2m", 2 * 1024**2),
        ("500K", 500 * 1024),
        ("1.5G", int(1.5 * 1024**3)),
        ("1.5 GiB", int(1.5 * 1024**3)),
        ("42", 42),
        ("2M/s", 2 * 1024**2),
        ("off", 0),
        ("unlimited", 0),
        ("0", 0),
        (1024, 1024),
        (None, 0),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "12q", "1.2.3", "-5M"])
def test_parse_size_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "—"),
        (-1, "—"),
        (0, "0s"),
        (42, "42s"),
        (60, "1m 00s"),
        (252, "4m 12s"),
        (2 * 3600 + 5 * 60, "2h 05m"),
        (86400 * 3 + 3600 * 4, "3d 4h"),
        (86400 * 45, "∞"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_fmt_eta_done_and_unknown():
    assert fmt_eta(0) == "✓"
    assert fmt_eta(-1) == "—"
    assert fmt_eta(90) == "1m 30s"


def test_fmt_ratio():
    assert fmt_ratio(1.234) == "1.23"
    assert fmt_ratio(None) == "—"
    assert fmt_ratio(123.4) == "123"


def test_fmt_percent_clamps():
    assert fmt_percent(0.427) == " 42.7%"
    assert fmt_percent(1.7) == "100.0%"
    assert fmt_percent(None) == "    —"
