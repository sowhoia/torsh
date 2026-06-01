"""Tests for configuration loading, normalisation and persistence."""
from __future__ import annotations

import importlib

import pytest
import yaml


@pytest.fixture
def config_mod(tmp_path, monkeypatch):
    """Reimport ``torsh.config`` with an isolated config dir per test."""
    monkeypatch.setenv("TORSH_CONFIG_DIR", str(tmp_path / "cfg"))
    import torsh.config as config

    importlib.reload(config)
    try:
        yield config
    finally:
        # Restore module-level state for any later imports.
        monkeypatch.delenv("TORSH_CONFIG_DIR", raising=False)
        importlib.reload(config)


def test_load_creates_file_with_defaults(config_mod):
    cfg = config_mod.load_config()
    assert config_mod.CONFIG_FILE.exists()
    assert cfg.rpc.host == "localhost"
    assert cfg.rpc.port == 9091
    assert cfg.ui.status_filter == "any"


def test_save_load_roundtrip(config_mod):
    cfg = config_mod.load_config()
    cfg.rpc.port = 12345
    cfg.ui.refresh_interval = 4.0
    cfg.ui.filter_text = "linux"
    config_mod.save_config(cfg)

    reloaded = config_mod.load_config()
    assert reloaded.rpc.port == 12345
    assert reloaded.ui.refresh_interval == 4.0
    assert reloaded.ui.filter_text == "linux"


def test_normalize_clamps_invalid_values(config_mod):
    cfg = config_mod.AppConfig()
    cfg.rpc.port = 999999          # out of range
    cfg.ui.refresh_interval = 999  # too large
    cfg.rpc.timeout = -5           # too small
    normalized = cfg.normalize()
    assert 1 <= normalized.rpc.port <= 65535
    assert normalized.ui.refresh_interval <= 30.0
    assert normalized.rpc.timeout >= 1.0


def test_corrupted_yaml_falls_back_to_defaults(config_mod):
    config_mod.ensure_config_dir(config_mod.CONFIG_DIR)
    config_mod.CONFIG_FILE.write_text("this: : not: valid: yaml: [")
    cfg = config_mod.load_config()  # must not raise
    assert cfg.rpc.host == "localhost"


def test_invalid_sort_column_is_dropped(config_mod):
    config_mod.ensure_config_dir(config_mod.CONFIG_DIR)
    config_mod.CONFIG_FILE.write_text(yaml.safe_dump({"ui": {"sort_column": 42}}))
    cfg = config_mod.load_config()
    assert cfg.ui.sort_column is None


def test_save_is_idempotent(config_mod):
    cfg = config_mod.load_config()
    config_mod.save_config(cfg)
    mtime = config_mod.CONFIG_FILE.stat().st_mtime_ns
    config_mod.save_config(cfg)  # identical payload -> no rewrite
    assert config_mod.CONFIG_FILE.stat().st_mtime_ns == mtime
