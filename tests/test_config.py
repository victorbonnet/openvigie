import logging
from pathlib import Path

import pytest

from openvigie.config import (
    CollectorSettings,
    ConfigError,
    HubSettings,
    config_dir,
    load_collector_settings,
    load_hub_settings,
    read_private_file,
    state_dir,
    write_private_file,
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_defaults_without_files(tmp_path: Path) -> None:
    assert load_hub_settings(tmp_path / "missing.toml") == HubSettings()
    collector = load_collector_settings(tmp_path / "missing.toml")
    assert collector == CollectorSettings()
    assert collector.active_window_s == 300


def test_hub_defaults_are_loopback() -> None:
    hub = HubSettings()
    assert hub.bind == "127.0.0.1"
    assert hub.allow_non_loopback is False


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.10", "100.64.0.1", "example.com"])  # noqa: S104
def test_non_loopback_bind_rejected_without_opt_in(tmp_path: Path, bind: str) -> None:
    path = write(tmp_path, "hub.toml", f'bind = "{bind}"\n')
    with pytest.raises(ConfigError, match="tailscale serve"):
        load_hub_settings(path)


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_binds_accepted(tmp_path: Path, bind: str) -> None:
    path = write(tmp_path, "hub.toml", f'bind = "{bind}"\n')
    assert load_hub_settings(path).bind == bind


def test_non_loopback_bind_with_opt_in_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = write(tmp_path, "hub.toml", 'bind = "0.0.0.0"\nallow_non_loopback = true\n')
    with caplog.at_level(logging.WARNING):
        assert load_hub_settings(path).bind == "0.0.0.0"  # noqa: S104
    assert "reachable from the network" in caplog.text


def test_unknown_key_rejected_with_its_name(tmp_path: Path) -> None:
    path = write(tmp_path, "collector.toml", "activ_window_s = 60\n")
    with pytest.raises(ConfigError, match="activ_window_s"):
        load_collector_settings(path)


@pytest.mark.parametrize(
    "text",
    ['active_window_s = "60"', "active_window_s = true", 'homes = "/home/pi"', "port = = 1"],
)
def test_wrong_types_rejected(tmp_path: Path, text: str) -> None:
    path = write(tmp_path, "collector.toml", text + "\n")
    with pytest.raises(ConfigError):
        load_collector_settings(path)


def test_collector_values_parsed(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "collector.toml",
        'homes = ["~/x", "/home/pi"]\nfast_tick_s = 2\nenable_plugins = ["acme"]\n',
    )
    settings = load_collector_settings(path)
    assert settings.homes == (Path("~/x").expanduser(), Path("/home/pi"))
    assert settings.fast_tick_s == 2.0
    assert settings.enable_plugins == ("acme",)


def test_invalid_window_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "collector.toml", "active_window_s = 0\n")
    with pytest.raises(ConfigError):
        load_collector_settings(path)


def test_env_overrides_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("OPENVIGIE_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert config_dir() == tmp_path / "cfg"
    assert state_dir() == tmp_path / "state" / "openvigie"


def test_private_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "token"
    write_private_file(path, "secret\n")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert read_private_file(path) == "secret"


def test_group_readable_secret_rejected(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("secret")
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="permissions too open"):
        read_private_file(path)


def test_symlinked_secret_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real"
    write_private_file(target, "secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        read_private_file(link)
