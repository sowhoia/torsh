"""Tests for the pure presentation helpers in the UI layer."""
from __future__ import annotations

from torsh.ui.app import format_percent, styled_ratio, styled_status
from torsh.ui.console import banner


def test_format_percent_fixed_width():
    assert format_percent(7.5) == "  7.5%"
    assert format_percent(100.0) == "100.0%"


def test_styled_status_known_and_unknown():
    assert styled_status("downloading").plain == "⬇ Downloading"
    assert styled_status("download pending").plain == "⏳ Download Pending"
    # Unknown statuses degrade gracefully rather than raising.
    assert "Frobnicating" in styled_status("frobnicating").plain


def test_styled_status_error_overrides():
    text = styled_status("downloading", error=True)
    assert text.plain == "⚠ Error"
    assert "red" in str(text.style)


def test_styled_ratio_colour_by_threshold():
    assert "green" in str(styled_ratio(2.0).style)
    assert "red" in str(styled_ratio(0.5).style)


def test_banner_builds_without_error():
    # Smoke test: the gradient/padding math must not blow up.
    assert banner() is not None
