"""Config: TOML roundtrip, coercion, dotted keys, legacy migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from torsh import config as cfg
from torsh import paths


def test_defaults_written_on_first_load(isolated_home):
    config = cfg.load()
    assert paths.config_file().exists()
    assert config.network.dht is True
    assert config.limits.download == 0
    assert config.downloads.resolved_dir().name == "torsh"


def test_roundtrip_preserves_values():
    config = cfg.load()
    config.limits.download = 2 * 1024**2
    config.ui.refresh_hz = 2.0
    config.downloads.sequential = True
    cfg.save(config)

    loaded = cfg.load()
    assert loaded.limits.download == 2 * 1024**2
    assert loaded.ui.refresh_hz == 2.0
    assert loaded.downloads.sequential is True


def test_corrupted_file_falls_back_to_defaults():
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("not [valid toml ===", encoding="utf-8")
    config = cfg.load()
    assert config.network.dht is True


def test_human_rates_accepted_in_file():
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[limits]\ndownload = "2M"\n', encoding="utf-8")
    config = cfg.load()
    assert config.limits.download == 2 * 1024**2


def test_out_of_range_values_clamped():
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("[network]\nport = 99999\n[ui]\nrefresh_hz = 500\n", encoding="utf-8")
    config = cfg.load()
    assert config.network.port == 65535
    assert config.ui.refresh_hz == 10.0


def test_nonsense_ui_settings_are_repaired(isolated_home):
    """A hand-edited sort key must be fixed here, not silently ignored by
    each frontend while `config list` keeps advertising it."""
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[ui]\nsort = "colour"\nstatus_filter = "sideways"\n', encoding="utf-8")
    config = cfg.load()
    assert config.ui.sort == "added"
    assert config.ui.status_filter == "all"

    # A descending marker on a real column is still legitimate.
    config.ui.sort = "-down"
    cfg.save(config)
    assert cfg.load().ui.sort == "-down"


def test_parse_error_names_the_problem(isolated_home):
    assert cfg.parse_error() is None  # no file yet
    cfg.load()
    assert cfg.parse_error() is None  # freshly written defaults

    paths.config_file().write_text("not [valid toml ===", encoding="utf-8")
    problem = cfg.parse_error()
    assert problem and "line" in problem.lower()


def test_saved_config_is_private(isolated_home):
    import stat

    cfg.load()
    assert stat.S_IMODE(paths.config_file().stat().st_mode) == 0o600


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("TORSH_DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("TORSH_PORT", "7001")
    config = cfg.load()
    assert config.downloads.resolved_dir() == tmp_path / "dl"
    assert config.network.port == 7001


def test_env_overrides_never_persisted(monkeypatch, tmp_path):
    """A one-off TORSH_* override must not be baked into config.toml."""
    monkeypatch.setenv("TORSH_DOWNLOAD_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("TORSH_PORT", "7001")

    effective = cfg.load()  # first run materialises the file
    assert effective.network.port == 7001

    on_disk = cfg.load(env=False)
    assert on_disk.network.port == 0
    assert on_disk.downloads.dir == ""

    # ...including through a load-modify-save cycle (`torsh config set`).
    on_disk.limits.download = 1024
    cfg.save(on_disk)
    saved = cfg.load(env=False)
    assert saved.limits.download == 1024
    assert saved.network.port == 0


def test_get_set_dotted_keys():
    config = cfg.load()
    assert cfg.get_key(config, "network.dht") is True

    assert cfg.set_key(config, "limits.download", "1.5M") == int(1.5 * 1024**2)
    assert cfg.set_key(config, "network.dht", "off") is False
    assert cfg.set_key(config, "ui.refresh_hz", "2") == 2.0
    assert cfg.set_key(config, "downloads.dir", "~/somewhere") == "~/somewhere"

    with pytest.raises(KeyError):
        cfg.get_key(config, "nope.nope")
    with pytest.raises(ValueError):
        cfg.set_key(config, "network.dht", "sideways")
    with pytest.raises(ValueError):
        cfg.set_key(config, "network.port", "not-a-port")
    with pytest.raises(ValueError):
        cfg.set_key(config, "limits.upload", "12 parrots")


def test_relative_download_dir_anchors_to_home():
    """A relative dir in the file must not depend on the daemon's cwd."""
    config = cfg.load()
    config.downloads.dir = "somewhere/deep"
    assert config.downloads.resolved_dir() == Path.home() / "somewhere/deep"
    config.downloads.dir = "~/explicit"
    assert config.downloads.resolved_dir() == Path.home() / "explicit"


def test_list_keys_covers_all_sections():
    config = cfg.load()
    keys = dict(cfg.list_keys(config))
    for expected in ("downloads.dir", "limits.ratio", "network.port", "daemon.autostart", "ui.sort"):
        assert expected in keys


def test_legacy_yaml_migration():
    legacy = paths.legacy_config_file()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "rpc:\n  host: localhost\npaths:\n  download_dir: /data/torrents\n",
        encoding="utf-8",
    )
    config = cfg.load()
    assert config.downloads.resolved_dir() == Path("/data/torrents")
