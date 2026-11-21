"""Tests for PW_CONFIG settings and the lowest-priority default file."""

import textwrap
from pathlib import Path

import yaml

from app import config as config_module
from app.config import Settings


def _clear_gateway_env(monkeypatch):
    for name in (
        "PW_CONFIG",
        "PW_GATEWAYS",
        "PW_HOST",
        "PW_GW_PWD",
        "PW_PASSWORD",
        "PW_EMAIL",
        "PW_AUTH_PATH",
        "PW_RSA_KEY_PATH",
        "PW_WIFI_HOST",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_block_and_server_block(tmp_path, monkeypatch):
    path = tmp_path / "gateways.yaml"
    path.write_text(
        textwrap.dedent("""
            server:
              host: 127.0.0.1
              port: 9999
            settings:
              failover: false
              mqtt_ha_discovery: false
              timeseries_daily_retention: "7"
              auth_mode: token
            gateways:
              - id: home
                host: 192.168.91.1
                gw_pwd: secret
            """),
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("PW_CONFIG", str(path))
    loaded = Settings()
    assert loaded.config_source == "pw_config"
    assert loaded.config_path == str(path)
    assert loaded.config_revision
    assert loaded.server_host == "127.0.0.1"
    assert loaded.server_port == 9999
    assert loaded.failover is False
    assert loaded.mqtt_ha_discovery is False
    assert loaded.timeseries_daily_retention == "7"
    assert loaded.auth_mode == "token"
    assert loaded.gateways[0].gw_pwd == "secret"


def test_settings_block_ignores_invalid_values_without_leaking(tmp_path, monkeypatch):
    path = tmp_path / "gateways.yaml"
    path.write_text(
        textwrap.dedent("""
            settings:
              failover: not-a-bool
              unknown_option: secret-value
              gateways: nope
            gateways:
              - id: home
                host: 192.168.91.1
                gw_pwd: secret
            """),
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("PW_CONFIG", str(path))
    loaded = Settings()
    assert loaded.failover is True
    assert len(loaded.gateways) == 1
    warnings = " ".join(loaded.config_warnings)
    assert "invalid settings.failover" in warnings
    assert "unknown settings key" in warnings
    assert "settings.gateways is controlled" in warnings
    assert "secret-value" not in warnings


def test_environment_wins_over_settings_block(tmp_path, monkeypatch):
    path = tmp_path / "gateways.yaml"
    path.write_text(
        textwrap.dedent("""
            settings:
              failover: false
              timeout: 99
            gateways:
              - id: home
                host: 192.168.91.1
                gw_pwd: secret
            """),
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("PW_FAILOVER", "yes")
    monkeypatch.setenv("PW_CONFIG", str(path))
    loaded = Settings()
    assert loaded.failover is True
    assert loaded.timeout == 99
    assert any(
        "set by the environment" in warning for warning in loaded.config_warnings
    )


def test_settings_siteid_accepts_integer(tmp_path, monkeypatch):
    path = tmp_path / "gateways.yaml"
    path.write_text(
        "settings:\n  siteid: 123\n"
        "gateways:\n  - id: home\n    host: 192.168.91.1\n    gw_pwd: secret\n",
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("PW_CONFIG", str(path))
    loaded = Settings()
    assert loaded.siteid == "123"


def test_default_file_precedes_auth_file_autodetection(tmp_path, monkeypatch):
    default_path = tmp_path / "default" / "gateways.yaml"
    default_path.parent.mkdir()
    default_path.write_text(
        "gateways:\n  - id: default-file\n    host: 192.168.91.1\n    gw_pwd: secret\n",
        encoding="utf-8",
    )
    authpath = tmp_path / "auth"
    authpath.mkdir()
    (authpath / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(config_module, "default_config_path", lambda: str(default_path))
    monkeypatch.setenv("PW_AUTH_PATH", str(authpath))
    loaded = Settings()
    assert loaded.config_source == "default_file"
    assert loaded.gateways[0].id == "default-file"


def test_default_file_is_used_only_without_explicit_source(tmp_path, monkeypatch):
    default_path = tmp_path / "default" / "gateways.yaml"
    default_path.parent.mkdir()
    default_path.write_text(
        "gateways:\n  - id: default-file\n    host: 192.168.91.1\n    gw_pwd: secret\n",
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(config_module, "default_config_path", lambda: str(default_path))
    loaded = Settings()
    assert loaded.config_source == "default_file"
    assert loaded.gateways[0].id == "default-file"


def test_empty_gateway_list_loads_without_config_error(tmp_path, monkeypatch):
    path = tmp_path / "gateways.yaml"
    path.write_text("gateways: []\n", encoding="utf-8")
    _clear_gateway_env(monkeypatch)
    monkeypatch.setenv("PW_CONFIG", str(path))
    loaded = Settings()
    assert loaded.config_source == "pw_config"
    assert loaded.gateways == []
    assert loaded.config_error is None


def test_pw_config_missing_does_not_fall_back(tmp_path, monkeypatch):
    default_path = tmp_path / "gateways.yaml"
    default_path.write_text(
        "gateways:\n  - id: default-file\n    host: 192.168.91.1\n    gw_pwd: secret\n",
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(config_module, "default_config_path", lambda: str(default_path))
    monkeypatch.setenv("PW_CONFIG", str(tmp_path / "missing.yaml"))
    loaded = Settings()
    assert loaded.config_source == "pw_config"
    assert loaded.gateways == []
    assert loaded.config_error


def test_pw_gateways_prevents_default_file(tmp_path, monkeypatch):
    default_path = tmp_path / "gateways.yaml"
    default_path.write_text(
        "gateways:\n  - id: default-file\n    host: 192.168.91.1\n    gw_pwd: secret\n",
        encoding="utf-8",
    )
    _clear_gateway_env(monkeypatch)
    monkeypatch.setattr(config_module, "default_config_path", lambda: str(default_path))
    monkeypatch.setenv(
        "PW_GATEWAYS",
        '[{"id":"env-gateway","host":"192.168.91.1","gw_pwd":"secret"}]',
    )
    loaded = Settings()
    assert loaded.config_source == "pw_gateways"
    assert loaded.gateways[0].id == "env-gateway"


def test_runtime_requirements_and_first_run_guidance():
    root = Path(__file__).resolve().parent.parent
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "pyyaml" in requirements.lower()
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    lines = [line.strip() for line in compose.splitlines()]
    assert "PW_HOST=192.168.91.1" not in lines
    assert "PW_GW_PWD=your-gateway-password" not in lines
    assert any(
        line.startswith("#") and "PW_HOST=192.168.91.1" in line for line in lines
    )
    assert any(
        line.startswith("#") and "PW_GW_PWD=your-gateway-password" in line
        for line in lines
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "Docker Compose users" in readme
    assert "environment-managed" in readme
    assert "/setup" in readme
