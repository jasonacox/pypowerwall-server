"""Coverage-focused tests for setup service branches that do not use Tesla."""

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import GatewayConfig, settings
from app.core import setup as setup_core
from app.models.setup import (
    SetupConfigureRequest,
    SetupDeleteConfigRequest,
    SetupEnvImportRequest,
    SetupGatewayInput,
    SetupScanRequest,
    SetupV1rAuthFileRequest,
)


@pytest.fixture
def core_env(monkeypatch, tmp_path):
    config_path = tmp_path / "gateways.yaml"
    authpath = tmp_path / "auth"
    authpath.mkdir()
    old_gateways = settings.gateways
    old_control = settings.control_secret
    old_private = dict(settings.__pydantic_private__)
    monkeypatch.setattr(setup_core, "default_config_path", lambda: str(config_path))
    monkeypatch.setattr(settings, "gateways", [])
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "pw_authpath", str(authpath), raising=False)
    settings.__pydantic_private__.update(
        {
            "_config_source": "none",
            "_config_path": None,
            "_config_error": None,
            "_config_warnings": [],
            "_config_revision": None,
            "_config_partial": False,
        }
    )
    monkeypatch.setattr(setup_core, "_SETUP_TOKEN", None)
    try:
        yield SimpleNamespace(config_path=config_path, authpath=authpath)
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)


def _gateway(**overrides):
    values = {"id": "home", "host": "192.168.91.1", "gw_pwd": "secret"}
    values.update(overrides)
    return GatewayConfig(**values)


def _request(gateways=None, options=None, expected_revision=None):
    return SetupConfigureRequest(
        gateways=gateways
        or [
            {
                "id": "home",
                "mode": "tedapi",
                "host": "192.168.91.1",
                "gw_pwd": "secret",
            }
        ],
        options=options or {},
        expected_revision=expected_revision,
    )


def test_env_import_preview_without_target_file(core_env):
    preview = setup_core.env_import_preview()
    assert preview["target_path"] == str(core_env.config_path)
    assert preview["revision"] is None


def test_env_import_preview_reads_existing_revision(core_env):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    preview = setup_core.env_import_preview()
    assert preview["revision"] == setup_core._revision(
        core_env.config_path.read_bytes()
    )


def test_env_import_activate_rejects_unmergeable_file(core_env):
    core_env.config_path.write_text("gateways: [", encoding="utf-8")
    settings._config_error = None
    revision = setup_core._revision(core_env.config_path.read_bytes())
    with pytest.raises(ValueError, match="cannot be merged"):
        setup_core.env_import_activate(
            SetupEnvImportRequest(expected_revision=revision)
        )


def test_delete_setup_config_creates_backup(core_env):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.delete_setup_config(
        SetupDeleteConfigRequest(expected_revision=revision)
    )
    assert result.deleted is True
    assert not core_env.config_path.exists()
    assert Path(result.backup_path).read_text(encoding="utf-8") == "gateways: []\n"
    with pytest.raises(ValueError, match="no configuration file"):
        setup_core.delete_setup_config(SetupDeleteConfigRequest())


def test_delete_setup_config_guards_managed_and_changed_files(core_env):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    settings._config_source = "pw_config"
    settings._config_path = str(core_env.config_path)
    with pytest.raises(ValueError, match="PW_CONFIG"):
        setup_core.delete_setup_config(SetupDeleteConfigRequest())

    settings._config_source = "default_file"
    with pytest.raises(ValueError, match="changed"):
        setup_core.delete_setup_config(
            SetupDeleteConfigRequest(expected_revision="wrong")
        )

    settings._config_source = "legacy_env"
    settings._config_path = None
    with pytest.raises(ValueError, match="environment variables"):
        setup_core.delete_setup_config(SetupDeleteConfigRequest())


def test_env_import_activate_requires_current_revision(core_env):
    with pytest.raises(ValueError, match="changed"):
        setup_core.env_import_activate(SetupEnvImportRequest(expected_revision="stale"))


def test_env_import_activate_repairs_malformed_file(core_env):
    core_env.config_path.write_text("gateways: [", encoding="utf-8")
    settings._config_error = "invalid"
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    assert yaml.safe_load(core_env.config_path.read_text())["gateways"] == []
    assert any("could not be parsed" in note for note in result.notes)


def test_configure_reports_malformed_config_repair(core_env):
    core_env.config_path.write_text("gateways: [", encoding="utf-8")
    settings._config_error = "invalid"
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(_request(expected_revision=revision))
    assert result.saved is True
    assert result.backup_path is not None
    assert any("could not be parsed" in warning for warning in result.warnings)


def test_env_import_activate_merges_mapping_blocks(core_env, monkeypatch):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "old"},
                "settings": {"failover": True},
                "gateways": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        setup_core,
        "_env_import_document",
        lambda: {
            "server": {"host": "new"},
            "gateways": [],
            "settings": {"failover": False},
        },
    )
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    assert doc["server"]["host"] == "new"
    assert doc["settings"]["failover"] is False


def test_env_import_activate_replaces_non_mapping_blocks(core_env, monkeypatch):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "server": "invalid",
                "settings": "invalid",
                "gateways": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        setup_core,
        "_env_import_document",
        lambda: {
            "server": {"host": "127.0.0.1"},
            "gateways": [],
            "settings": {"failover": False},
        },
    )
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text())
    assert doc["server"]["host"] == "127.0.0.1"
    assert doc["settings"]["failover"] is False


def test_env_import_flags_environment_settings(core_env, monkeypatch):
    monkeypatch.setenv("PW_TIMEOUT", "7")
    preview = setup_core.env_import_preview()
    assert preview["requires_removal"] is True
    assert any("PW_TIMEOUT" in note for note in preview["notes"])


def test_env_import_ignores_server_block_overrides(core_env, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["http://example.com"]')
    monkeypatch.setattr(setup_core, "_WRITABLE_OPTION_FIELDS", {"cors_origins"})
    preview = setup_core.env_import_preview()
    assert preview["requires_removal"] is False


def test_env_import_without_writable_options(core_env, monkeypatch):
    monkeypatch.setattr(setup_core, "_WRITABLE_OPTION_FIELDS", set())
    preview = setup_core.env_import_preview()
    assert "settings" not in preview["yaml"]
    result = setup_core.env_import_activate(SetupEnvImportRequest())
    assert result.saved is True
    assert "settings" not in yaml.safe_load(
        core_env.config_path.read_text(encoding="utf-8")
    )


def test_setup_token_lifecycle_and_constant_time_verification(core_env, monkeypatch):
    monkeypatch.setattr(settings, "control_secret", None)
    setup_core.initialize_setup_auth()
    token = setup_core._SETUP_TOKEN
    assert token
    assert setup_core.setup_auth_token() == token
    assert setup_core.verify_setup_token(token) is True
    assert setup_core.verify_setup_token(None) is False
    assert setup_core.verify_setup_token("wrong") is False

    monkeypatch.setattr(settings, "control_secret", "admin-token")
    setup_core.initialize_setup_auth()
    assert setup_core._SETUP_TOKEN is None
    assert setup_core.setup_auth_token() is None
    assert setup_core.verify_setup_token("admin-token") is True
    assert setup_core.verify_setup_token(token) is False


def test_error_helpers_and_read_doc(tmp_path):
    assert setup_core._masked_error(None) is None
    assert setup_core._masked_error("  one\n two  ") == "one two"
    assert len(setup_core._masked_error("x" * 500)) == 300

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert setup_core._read_doc(str(empty))[0] == {}
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("- one\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        setup_core._read_doc(str(scalar))
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("gateways: [", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        setup_core._read_doc(str(invalid))


def test_writable_and_effective_path_branches(core_env, monkeypatch):
    assert setup_core._is_writable(str(core_env.config_path)) is True
    assert (
        setup_core._is_writable(str(core_env.config_path.parent / "new" / "x.yaml"))
        is True
    )
    monkeypatch.setattr(setup_core.os, "access", lambda *args: False)
    assert setup_core._is_writable(str(core_env.config_path)) is False

    settings._config_source = "none"
    assert setup_core._effective_path() == str(core_env.config_path)
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    assert setup_core._effective_path() == str(core_env.config_path)
    settings._config_source = "pw_config"
    assert setup_core._effective_path() == str(core_env.config_path)
    settings._config_source = "legacy_env"
    assert setup_core._effective_path() is None


@pytest.mark.parametrize(
    "message,expected",
    [
        (None, False),
        ("timeout", False),
        ("401 Unauthorized", True),
        ("403 forbidden", True),
        ("access denied", True),
        ("login failed", True),
        ("invalid password", True),
        ("check your password", True),
        ("authentication failed", True),
    ],
)
def test_auth_error_detection(message, expected):
    assert setup_core._is_auth_error(message) is expected


@pytest.mark.parametrize(
    "config,expected",
    [
        (_gateway(fleetapi=True), "Hybrid FleetAPI"),
        (GatewayConfig(id="fleet", fleetapi=True), "FleetAPI"),
        (_gateway(cloud_mode=True), "Cloud"),
        (_gateway(rsa_key_path="/data/key.pem"), "TEDAPI v1r"),
        (_gateway(password="customer"), "Hybrid TEDAPI"),
        (_gateway(), "TEDAPI"),
        (
            GatewayConfig(id="local", host="192.168.91.1", password="customer"),
            "Local/Basic LAN",
        ),
        (GatewayConfig(id="email", email="owner@example.com"), "Hybrid"),
        (GatewayConfig(id="blank"), "Unknown"),
    ],
)
def test_derive_mode(config, expected):
    assert setup_core._derive_mode(config) == expected
    assert setup_core._derive_mode(config, "Live") == "Live"


@pytest.mark.parametrize(
    "config,expected",
    [
        (_gateway(fleetapi=True), "hybrid_fleetapi"),
        (GatewayConfig(id="fleet", fleetapi=True), "fleetapi"),
        (_gateway(cloud_mode=True), "cloud"),
        (_gateway(rsa_key_path="/data/key.pem"), "tedapi_v1r"),
        (_gateway(password="customer"), "hybrid_tedapi"),
        (_gateway(), "tedapi"),
        (GatewayConfig(id="local", host="192.168.91.1", password="customer"), "local"),
        (
            GatewayConfig(id="email", host="192.168.91.1", email="owner@example.com"),
            "hybrid",
        ),
        (
            GatewayConfig(
                id="hybrid",
                host="192.168.91.1",
                password="customer",
                email="owner@example.com",
                authpath="/data",
            ),
            "hybrid",
        ),
        (GatewayConfig(id="blank"), "tedapi"),
    ],
)
def test_setup_mode(config, expected):
    assert setup_core._setup_mode(config) == expected


def test_hybrid_fleetapi_roundtrip_preserves_local_fields(core_env):
    item = SetupGatewayInput(
        id="home",
        mode="hybrid_fleetapi",
        host="192.168.91.1",
        gw_pwd="secret",
        email="owner@example.com",
        authpath=str(core_env.authpath),
    )
    issues = []
    config = setup_core._validate_gateway(item, {}, issues)
    assert config is not None
    assert not [issue for issue in issues if issue.severity == "error"]
    assert config.host == "192.168.91.1"
    assert config.gw_pwd == "secret"
    assert config.fleetapi is True
    assert config.cloud_mode is False
    editable = setup_core._editable_gateway(config)
    assert editable["mode"] == "hybrid_fleetapi"
    assert editable["host"] == "192.168.91.1"
    assert setup_core._setup_mode(config) == "hybrid_fleetapi"


def test_hybrid_fleetapi_requires_email_and_local_credential(core_env):
    item = SetupGatewayInput(
        id="home",
        mode="hybrid_fleetapi",
        host="192.168.91.1",
        authpath=str(core_env.authpath),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    fields = {issue.field for issue in issues if issue.severity == "error"}
    assert "password" in fields
    assert "email" in fields


def test_hybrid_fleetapi_mode_is_preserved_in_setup_status(core_env):
    config = GatewayConfig(
        id="hybrid",
        host="192.168.91.1",
        gw_pwd="secret",
        email="owner@example.com",
        authpath=str(core_env.authpath),
        fleetapi=True,
    )
    settings.gateways = [config]
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    status = setup_core.get_setup_status()
    assert status.gateway_configs[0]["mode"] == "hybrid_fleetapi"


def test_hybrid_mode_is_preserved_in_setup_status(core_env):
    config = GatewayConfig(
        id="hybrid",
        host="192.168.91.1",
        password="customer",
        email="owner@example.com",
        authpath=str(core_env.authpath),
    )
    settings.gateways = [config]
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    status = setup_core.get_setup_status()
    assert status.gateway_configs[0]["mode"] == "hybrid"


def test_editable_gateway_never_contains_secrets():
    config = _gateway(password="customer", email="owner@example.com", authpath="/data")
    data = setup_core._editable_gateway(config)
    assert data["mode"] == "hybrid_tedapi"
    assert "gw_pwd" not in data
    assert "password" not in data
    assert "cloud_mode" not in data
    assert "fleetapi" not in data
    assert data["email"] == "owner@example.com"


def test_status_states_and_gateway_summary(core_env, monkeypatch):
    from app.core.gateway_manager import gateway_manager

    settings._config_source = "none"
    status = setup_core.get_setup_status()
    assert status.state == "unconfigured"
    assert status.setup_recommended is True
    assert status.can_write is True

    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = "broken"
    status = setup_core.get_setup_status()
    assert status.state == "invalid"
    assert status.config_writable is True

    settings._config_source = "pw_gateways"
    settings._config_error = None
    status = setup_core.get_setup_status()
    assert status.state == "env_managed"
    assert status.can_write is False

    settings.gateways = [_gateway()]
    settings._config_source = "default_file"
    settings._config_partial = True
    settings._config_error = None
    status = setup_core.get_setup_status()
    assert status.state == "partial"

    settings._config_partial = False
    status = setup_core.get_setup_status()
    assert status.state == "unreachable"
    assert status.gateways[0].online is False

    gateway_manager.cache["home"] = SimpleNamespace(
        online=True,
        error=None,
        data=SimpleNamespace(connection_mode="Cloud"),
    )
    status = setup_core.get_setup_status()
    assert status.state == "configured"
    assert status.gateways[0].mode == "Cloud"

    gateway_manager.cache["home"] = SimpleNamespace(
        online=False,
        error="401 Unauthorized",
        data=None,
    )
    status = setup_core.get_setup_status()
    assert status.state == "auth_failed"
    assert status.gateways[0].last_error == "401 Unauthorized"

    gateway_manager.cache["home"] = SimpleNamespace(
        online=False,
        error="connection timeout",
        data=None,
    )
    status = setup_core.get_setup_status()
    assert status.state == "unreachable"


def test_status_masks_secrets_and_reads_settings_keys(core_env, monkeypatch):
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings.gateways = [_gateway()]
    settings.control_secret = "super-secret"
    monkeypatch.setattr(settings, "mqtt_password", "mqtt-secret")
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "settings": {"failover": False, "mqtt_ha_discovery": False},
                "gateways": [{"id": "home", "host": "192.168.91.1"}],
            }
        ),
        encoding="utf-8",
    )
    status = setup_core.get_setup_status()
    assert status.settings["control_secret"] == "***"
    assert status.settings["mqtt_password"] == "***"
    assert status.settings_keys == ["failover", "mqtt_ha_discovery"]
    assert status.control_enabled is True
    assert any("PW_CONTROL_SECRET" in warning for warning in status.warnings)


def test_status_survives_malformed_settings_block(core_env):
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = "invalid"
    core_env.config_path.write_text("settings: [broken", encoding="utf-8")
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    status = setup_core.get_setup_status()
    assert status.settings_keys == []
    assert status.state == "invalid"


def test_status_reports_pending_restart_after_save(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {"gateways": [{"id": "home", "host": "192.168.91.1", "gw_pwd": "secret"}]}
        ),
        encoding="utf-8",
    )
    settings._config_source = "none"
    settings._config_path = None
    settings.gateways = []
    status = setup_core.get_setup_status()
    assert status.state == "restart_required"
    assert status.restart_required is True
    assert any("changed since startup" in warning for warning in status.warnings)


def test_status_restart_flag_clears_after_reload_and_detects_changes(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {"gateways": [{"id": "home", "host": "192.168.91.1", "gw_pwd": "secret"}]}
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings.gateways = [_gateway()]
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    status = setup_core.get_setup_status()
    assert status.restart_required is False
    assert status.state != "restart_required"

    core_env.config_path.write_text(
        yaml.safe_dump(
            {"gateways": [{"id": "home", "host": "192.168.91.1", "gw_pwd": "changed"}]}
        ),
        encoding="utf-8",
    )
    status = setup_core.get_setup_status()
    assert status.restart_required is True
    assert status.state == "restart_required"


def test_empty_gateway_file_is_unconfigured(core_env):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    status = setup_core.get_setup_status()
    assert status.state == "unconfigured"
    assert status.can_write is True
    assert status.config_file_revision
    assert status.setup_recommended is True


def test_status_restart_flag_ignores_env_managed_config(core_env):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    settings._config_source = "pw_gateways"
    settings._config_path = None
    status = setup_core.get_setup_status()
    assert status.restart_required is False
    assert status.state == "env_managed"


def test_validate_host_branches():
    issues = []
    setup_core._validate_host(None, issues)
    assert issues[0].field == "host"
    assert "required" in issues[0].message

    for value in ("bad host", "http://gateway", "gateway/path", "gateway\\path"):
        issues = []
        setup_core._validate_host(value, issues)
        assert issues

    issues = []
    setup_core._validate_host("8.8.8.8", issues)
    assert issues == []
    issues = []
    setup_core._validate_host("192.168.91.1", issues)
    assert issues == []
    issues = []
    setup_core._validate_host("gateway.local", issues)
    assert issues == []


def test_validate_path_accepts_existing_paths(core_env):
    issues = []
    setup_core._validate_path("authpath", str(Path.cwd() / "README.md"), issues)
    assert issues == []

    issues = []
    setup_core._validate_path(
        "authpath", str(Path.cwd() / "README.md"), issues, directory=True
    )
    assert "not a directory" in issues[0].message

    issues = []
    setup_core._validate_path(
        "rsa_key_path", str(core_env.authpath / "missing.pem"), issues, must_exist=True
    )
    assert "does not exist" in issues[0].message

    regular = core_env.authpath / "regular"
    regular.write_text("x", encoding="utf-8")
    issues = []
    setup_core._validate_path("authpath", str(regular), issues, directory=True)
    assert "not a directory" in issues[0].message

    issues = []
    setup_core._validate_path("rsa_key_path", str(core_env.authpath), issues)
    assert "not a file" in issues[0].message

    issues = []
    setup_core._validate_path(None, None, issues)
    assert issues == []


@pytest.mark.parametrize(
    "gateway,expected_field",
    [
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"gw_pwd": None}, "gw_pwd"),
    ],
)
def test_validate_gateway_local_errors(core_env, gateway, expected_field):
    values = {"id": "home", "mode": "tedapi", "host": "192.168.91.1"}
    values.update(gateway)
    item = SetupGatewayInput(**values)
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == expected_field for issue in issues)


@pytest.mark.parametrize(
    "mode,expected_field",
    [
        ("local", "password"),
        ("basic_lan", "password"),
        ("hybrid_tedapi", "password"),
        ("hybrid", "email"),
        ("hybrid_fleetapi", "password"),
        ("cloud", "email"),
        ("tedapi_v1r", "rsa_key_path"),
    ],
)
def test_validate_gateway_required_credentials(core_env, mode, expected_field):
    item = SetupGatewayInput(
        id="home",
        mode=mode,
        host="192.168.91.1",
        siteid="123",
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == expected_field for issue in issues)


def test_validate_gateway_email_and_authpath_requirements(core_env, monkeypatch):
    monkeypatch.setattr(settings, "pw_authpath", None)
    item = SetupGatewayInput(
        id="home",
        mode="local",
        host="192.168.91.1",
        password="customer",
        email="owner@example.com",
    )
    issues = []
    config = setup_core._validate_gateway(item, {}, issues)
    assert config is not None
    assert config.authpath is None
    assert not [issue for issue in issues if issue.severity == "error"]

    item = SetupGatewayInput(
        id="cloud",
        mode="cloud",
        email="owner@example.com",
        authpath=str(core_env.authpath),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is not None
    assert issues == []

    item = SetupGatewayInput(
        id="cloud-no-email",
        mode="cloud",
        authpath=str(core_env.authpath),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == "email" for issue in issues)

    monkeypatch.setattr(settings, "pw_authpath", None)
    item = SetupGatewayInput(
        id="cloud-no-auth",
        mode="cloud",
        email="owner@example.com",
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == "authpath" for issue in issues)


def test_validate_gateway_explicit_clear_drops_existing_auth(core_env):
    item = SetupGatewayInput(
        id="home",
        mode="local",
        host="192.168.91.1",
        password="customer",
        email=None,
        authpath=None,
    )
    issues = []
    config = setup_core._validate_gateway(
        item,
        {"email": "owner@example.com", "authpath": str(core_env.authpath)},
        issues,
    )
    assert config is not None
    assert config.email is None
    assert config.authpath is None


def test_validate_local_email_without_authpath_is_allowed(core_env, monkeypatch):
    monkeypatch.setattr(settings, "pw_authpath", None, raising=False)
    item = SetupGatewayInput(
        id="local",
        mode="local",
        host="192.168.91.1",
        password="customer",
        email="owner@example.com",
    )
    issues = []
    config = setup_core._validate_gateway(item, {}, issues)
    assert config is not None
    assert config.authpath is None
    assert not [issue for issue in issues if issue.severity == "error"]
    assert not [issue for issue in issues if issue.field == "authpath"]

    monkeypatch.setattr(settings, "control_secret", "token")
    issues = []
    config = setup_core._validate_gateway(item, {}, issues)
    assert config is not None
    assert any(
        issue.field == "authpath" and issue.severity == "warning" for issue in issues
    )


def test_validate_cloud_email_still_requires_authpath(core_env, monkeypatch):
    monkeypatch.setattr(settings, "pw_authpath", None, raising=False)
    item = SetupGatewayInput(id="cloud", mode="cloud", email="owner@example.com")
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(
        issue.field == "authpath" and issue.severity == "error" for issue in issues
    )


def test_validate_gateway_path_errors(core_env):
    key = core_env.authpath / "missing.pem"
    item = SetupGatewayInput(
        id="v1r",
        mode="tedapi_v1r",
        host="192.168.91.1",
        password="customer",
        rsa_key_path=str(key),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == "rsa_key_path" for issue in issues)

    file_path = core_env.authpath / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    item = SetupGatewayInput(
        id="cloud",
        mode="cloud",
        email="owner@example.com",
        authpath=str(file_path),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == "authpath" for issue in issues)


@pytest.mark.parametrize(
    "mode,gateway,expected_mode",
    [
        (
            "local",
            {"host": "192.168.91.1", "password": "customer"},
            "Local/Basic LAN",
        ),
        (
            "basic_lan",
            {"host": "192.168.91.1", "password": "customer"},
            "Local/Basic LAN",
        ),
        (
            "hybrid_tedapi",
            {"host": "192.168.91.1", "gw_pwd": "secret", "password": "customer"},
            "Hybrid TEDAPI",
        ),
        (
            "tedapi_v1r",
            {
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
                "wifi_host": "192.168.91.2",
            },
            "TEDAPI v1r",
        ),
        (
            "hybrid",
            {
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
                "wifi_host": "192.168.91.2",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "Hybrid",
        ),
        (
            "cloud",
            {
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "Cloud",
        ),
        (
            "fleetapi",
            {
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "FleetAPI",
        ),
        (
            "hybrid",
            {
                "host": "192.168.91.1",
                "password": "customer",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "Hybrid",
        ),
        (
            "hybrid",
            {
                "host": "192.168.91.1",
                "gw_pwd": "secret",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "Hybrid",
        ),
    ],
)
def test_validate_gateway_constructs_each_mode(core_env, mode, gateway, expected_mode):
    key = core_env.authpath / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    values = {"id": "home", "mode": mode, "host": "192.168.91.1"}
    values.update(gateway)
    for name, value in list(values.items()):
        if value == "KEY":
            values[name] = str(key)
        elif value == "AUTHPATH":
            values[name] = str(core_env.authpath)
    issues = []
    config = setup_core._validate_gateway(SetupGatewayInput(**values), {}, issues)
    assert issues == []
    assert config is not None
    assert setup_core._derive_mode(config) == expected_mode
    if mode == "tedapi_v1r":
        assert config.wifi_host == "192.168.91.2"


def test_setup_rejects_out_of_range_numeric_options(core_env):
    for name, value in (
        ("server_port", 0),
        ("server_port", 70000),
        ("https_port", 0),
        ("cache_expire", -1),
        ("timeout", 0),
        ("mqtt_qos", 3),
        ("tedapi_probe_interval", 4),
    ):
        result, _, options = setup_core.validate_setup_request(
            _request(options={name: value}), {}
        )
        assert result.valid is False, name
        assert any(issue.field == f"options.{name}" for issue in result.errors)
        assert name not in options


def test_validate_warns_when_environment_wins(core_env, monkeypatch):
    monkeypatch.setenv("PW_TIMEOUT", "7")
    result, _, _ = setup_core.validate_setup_request(
        _request(options={"timeout": 9}), {}
    )
    assert any(
        issue.field == "options.timeout" and issue.severity == "warning"
        for issue in result.warnings
    )


def test_validate_gateway_construction_failure_is_reported(core_env, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("bad config")

    monkeypatch.setattr(setup_core, "GatewayConfig", broken)
    item = SetupGatewayInput(
        id="home", mode="tedapi", host="192.168.91.1", gw_pwd="secret"
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(issue.field == "gateway" for issue in issues)


def test_validate_setup_request_preserves_secret_on_rename(core_env):
    existing_doc = {
        "gateways": [
            {
                "id": "old",
                "host": "192.168.91.1",
                "gw_pwd": "stored",
                "timezone": "America/Los_Angeles",
            }
        ]
    }
    result, configs, _ = setup_core.validate_setup_request(
        SetupConfigureRequest(
            gateways=[
                {
                    "id": "new",
                    "original_id": "old",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                }
            ]
        ),
        existing_doc,
    )
    assert result.valid is True
    assert configs[0].id == "new"
    assert configs[0].gw_pwd == "stored"


def test_validate_setup_request_errors_and_warnings(core_env):
    result, configs, options = setup_core.validate_setup_request(
        SetupConfigureRequest(gateways=[]), {}
    )
    assert result.valid is True
    assert any(
        issue.field == "gateways" and issue.severity == "warning"
        for issue in result.warnings
    )
    assert configs == [] and options == {}

    result, _, _ = setup_core.validate_setup_request(
        SetupConfigureRequest(
            gateways=[
                {"id": "home", "mode": "tedapi", "host": "192.168.91.1", "gw_pwd": "a"},
                {"id": "home", "mode": "tedapi", "host": "192.168.91.1", "gw_pwd": "b"},
            ]
        ),
        {},
    )
    assert any("Duplicate" in issue.message for issue in result.errors)

    result, _, _ = setup_core.validate_setup_request(
        _request(options={"server_port": "not-a-port"}), {}
    )
    assert any(issue.field == "options.server_port" for issue in result.errors)

    result, _, options = setup_core.validate_setup_request(
        _request(options={"cache_file": str(Path.cwd() / "outside.cache")}), {}
    )
    assert not any(issue.field == "options.cache_file" for issue in result.errors)
    assert options["cache_file"] == str(Path.cwd() / "outside.cache")

    result, _, _ = setup_core.validate_setup_request(
        _request(options={"https_mode": True, "https_cert": "/data/cert.pem"}), {}
    )
    assert any(issue.field == "options.https" for issue in result.errors)

    result, _, _ = setup_core.validate_setup_request(
        _request(
            options={
                "mqtt_controls": 1,
                "battery_dispatch": True,
                "cloud_read_fallback": True,
                "wifi_v1r_fallback": True,
                "local_first": True,
            }
        ),
        {},
    )
    assert result.valid is True
    warning_fields = {issue.field for issue in result.warnings}
    assert {
        "options.mqtt_controls",
        "options.battery_dispatch",
        "options.cloud",
        "options.wifi_v1r_fallback",
        "options.local_first",
    } <= warning_fields

    result, _, _ = setup_core.validate_setup_request(
        _request(
            options={
                "https_cert": str(core_env.authpath / "missing-cert.pem"),
                "mqtt_tls_ca_cert": str(core_env.authpath / "missing-ca.pem"),
            }
        ),
        {},
    )
    warning_fields = {issue.field for issue in result.warnings}
    assert "options.https_cert" in warning_fields
    assert "options.mqtt_tls_ca_cert" in warning_fields


def test_validate_setup_request_accepts_valid_options(core_env):
    cert = core_env.authpath / "cert.pem"
    key = core_env.authpath / "key.pem"
    ca = core_env.authpath / "ca.pem"
    for path in (cert, key, ca):
        path.write_text("x", encoding="utf-8")
    result, configs, options = setup_core.validate_setup_request(
        _request(
            options={
                "server_port": 9443,
                "failover": False,
                "https_mode": True,
                "https_cert": str(cert),
                "https_key": str(key),
                "mqtt_tls_ca_cert": str(ca),
                "cors_origins": ["https://example.com"],
            }
        ),
        {},
    )
    assert result.valid is True
    assert result.errors == []
    assert options["server_port"] == 9443
    assert options["failover"] is False
    assert configs[0].id == "home"


def test_load_existing_config_branches(core_env, monkeypatch):
    assert setup_core.load_existing_config() == {}
    core_env.config_path.write_text("gateways: []", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    assert setup_core.load_existing_config()["gateways"] == []
    core_env.config_path.write_text("broken: [", encoding="utf-8")
    assert setup_core.load_existing_config() == {}
    monkeypatch.setattr(setup_core, "_effective_path", lambda: None)
    assert setup_core.load_existing_config() == {}


def test_write_atomic_backup_and_revision(core_env):
    path = core_env.config_path
    revision, backup = setup_core._write_atomic(
        str(path), {"gateways": [{"id": "first"}]}
    )
    assert len(revision) == 64
    assert backup is None
    revision, backup = setup_core._write_atomic(
        str(path), {"gateways": [{"id": "second"}]}
    )
    assert backup == str(path) + ".bak"
    assert Path(backup).is_file()
    assert yaml.safe_load(path.read_text())["gateways"][0]["id"] == "second"


def test_apply_setup_errors_and_option_merging(core_env, monkeypatch):
    settings._config_source = "pw_config"
    settings._config_path = None
    with pytest.raises(ValueError, match="no writable"):
        setup_core.apply_setup_request(_request())

    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    with pytest.raises(ValueError, match="changed"):
        setup_core.apply_setup_request(_request(expected_revision="missing"))

    core_env.config_path.write_text("gateways: [", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be merged"):
        setup_core.apply_setup_request(_request())

    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8000},
                "settings": {"failover": True},
                "gateways": [{"id": "old", "host": "192.168.91.9"}],
            }
        ),
        encoding="utf-8",
    )
    revision = hashlib.sha256(core_env.config_path.read_bytes()).hexdigest()
    result = setup_core.apply_setup_request(
        _request(
            options={"server_port": 9000, "failover": False},
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text())
    assert doc["server"]["port"] == 9000
    assert doc["settings"]["failover"] is False
    assert doc["gateways"][0]["id"] == "home"
    assert result.backup_path


def test_apply_setup_returns_warnings(core_env):
    result = setup_core.apply_setup_request(_request(options={"mqtt_controls": 1}))
    assert result.saved is True
    assert any("control token" in warning for warning in result.warnings)


def test_auth_status_paths_and_readiness(core_env):
    outside = setup_core.auth_status("cloud", str(Path.cwd() / "README.md"))
    assert outside["ready"] is False
    assert outside["authpath"] is not None

    missing = setup_core.auth_status("cloud", str(core_env.authpath))
    assert missing["ready"] is False
    assert missing["command"] == "python -m pypowerwall setup"

    (core_env.authpath / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    (core_env.authpath / ".pypowerwall.site").write_text("123", encoding="utf-8")
    ready = setup_core.auth_status("cloud", str(core_env.authpath))
    assert ready["ready"] is True
    assert ready["authpath"] == str(core_env.authpath)

    (core_env.authpath / ".pypowerwall.fleetapi").write_text("{}", encoding="utf-8")
    fleet = setup_core.auth_status("fleetapi", str(core_env.authpath))
    assert fleet["ready"] is True
    assert fleet["command"] == "python -m pypowerwall setup -fleetapi"


def test_auth_status_accepts_global_siteid(core_env, monkeypatch):
    (core_env.authpath / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "siteid", "123")
    ready = setup_core.auth_status("cloud", str(core_env.authpath))
    assert ready["ready"] is True


def test_v1r_result_state_messages(core_env):
    root = core_env.authpath
    kwargs = {
        "root": root,
        "fingerprint": "f",
        "private_key_path": root / "private.pem",
        "public_key_path": root / "public.der",
        "siteid": "123",
        "email": "owner@example.com",
    }
    pending = setup_core._v1r_result(state=1, **kwargs)
    assert pending.pending is True and pending.proof_hint
    verified = setup_core._v1r_result(state=3, **kwargs)
    assert verified.verified is True and verified.proof_hint
    timed_out = setup_core._v1r_result(state=2, **kwargs)
    assert timed_out.timed_out is True
    removed = setup_core._v1r_result(state=4, **kwargs)
    assert removed.removed is True
    unknown = setup_core._v1r_result(state=9, **kwargs)
    assert unknown.message.startswith("Key state:")
    no_state = setup_core._v1r_result(state=None, **kwargs)
    assert "No key state" in no_state.message
    needs_site = setup_core._v1r_result(state=None, needs_site=True, **kwargs)
    assert needs_site.needs_site is True
    assert needs_site.proof_hint is None


def test_v1r_rejected_matches_errors():
    assert setup_core._v1r_rejected(ValueError("token rejected")) is True
    assert setup_core._v1r_rejected(ValueError("HTTP 401")) is True
    assert setup_core._v1r_rejected(ValueError("timeout")) is False


def test_v1r_auth_token_errors_and_valid_token(core_env, monkeypatch):
    root = core_env.authpath
    with pytest.raises(ValueError, match="login required"):
        setup_core._v1r_auth_token(root)
    (root / ".pypowerwall.auth").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="unable to read"):
        setup_core._v1r_auth_token(root)
    (root / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no account"):
        setup_core._v1r_auth_token(root)
    (root / ".pypowerwall.auth").write_text(
        json.dumps(
            {
                "owner@example.com": {
                    "sso": {
                        "access_token": "valid-token",
                        "expires_at": int(time.time() + 3600),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert setup_core._v1r_auth_token(root) == ("valid-token", "owner@example.com")
    with pytest.raises(ValueError, match="not in the auth file"):
        setup_core._v1r_auth_token(root, "other@example.com")


def test_v1r_refresh_auth_success_and_errors(core_env, monkeypatch):
    root = core_env.authpath
    auth = root / ".pypowerwall.auth"

    with pytest.raises(ValueError, match="unable to read"):
        setup_core._v1r_refresh_auth(root)
    auth.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no account"):
        setup_core._v1r_refresh_auth(root)
    auth.write_text(json.dumps({"owner@example.com": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="no usable account"):
        setup_core._v1r_refresh_auth(root)
    auth.write_text(json.dumps({"owner@example.com": {"sso": {}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="expired"):
        setup_core._v1r_refresh_auth(root)
    with pytest.raises(ValueError, match="not in the auth file"):
        setup_core._v1r_refresh_auth(root, "other@example.com")

    auth.write_text(
        json.dumps({"owner@example.com": {"sso": {"refresh_token": "refresh-token"}}}),
        encoding="utf-8",
    )

    def fail_refresh(token):
        raise RuntimeError("nope")

    import pypowerwall.tesla_auth as auth_module

    monkeypatch.setattr(auth_module, "_refresh_access_token", fail_refresh)
    with pytest.raises(ValueError, match="could not be refreshed"):
        setup_core._v1r_refresh_auth(root)

    monkeypatch.setattr(
        auth_module, "_refresh_access_token", lambda token: {"expires_in": 60}
    )
    with pytest.raises(ValueError, match="could not be refreshed"):
        setup_core._v1r_refresh_auth(root)

    monkeypatch.setattr(
        auth_module,
        "_refresh_access_token",
        lambda token: {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 60,
        },
    )
    token, email = setup_core._v1r_refresh_auth(root, "owner@example.com")
    assert (token, email) == ("new-token", "owner@example.com")
    saved = json.loads(auth.read_text(encoding="utf-8"))["owner@example.com"]["sso"]
    assert saved["access_token"] == "new-token"
    assert (root / ".pypowerwall.auth.bak").is_file()


def test_v1r_auth_file_validation(core_env):
    root = core_env.authpath
    with pytest.raises(ValueError, match="empty"):
        setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content=" ")
        )
    with pytest.raises(ValueError, match="too large"):
        setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content="x" * (256 * 1024 + 1))
        )
    with pytest.raises(ValueError, match="no Tesla account"):
        setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(authpath=str(root), content="{}")
        )
    with pytest.raises(ValueError, match="no usable"):
        setup_core.v1r_auth_file(
            SetupV1rAuthFileRequest(
                authpath=str(root),
                content=json.dumps({"owner@example.com": {"sso": {}}}),
            )
        )


def test_v1r_sites_errors_and_filtering(core_env, monkeypatch):
    import pypowerwall.v1r_register as register_module

    def fake(code, response):
        monkeypatch.setattr(
            register_module, "api_call", lambda *args, **kwargs: (code, response)
        )
        with pytest.raises(ValueError):
            setup_core._v1r_sites("token")

    fake(401, {})
    fake(500, {})
    fake(200, [])
    monkeypatch.setattr(
        register_module,
        "api_call",
        lambda *args, **kwargs: (
            200,
            {
                "response": [
                    "not-a-dict",
                    {"site_name": "no-id"},
                    {"energy_site_id": 123, "site_name": "Home"},
                ]
            },
        ),
    )
    sites = setup_core._v1r_sites("token")
    assert len(sites) == 1
    assert sites[0].siteid == "123"


def test_scan_gateways_errors_and_empty_results(core_env, monkeypatch):
    with pytest.raises(ValueError, match="IPv4"):
        setup_core.scan_gateways(SetupScanRequest(cidr="not-a-network"))
    import pypowerwall.scan as scan_module

    monkeypatch.setattr(scan_module, "get_my_ip", lambda: "192.168.1.42")
    monkeypatch.setattr(scan_module, "scan", lambda **kwargs: [])
    result = setup_core.scan_gateways(SetupScanRequest())
    assert result.ok is True
    assert result.network == "192.168.1.0/24"
    assert "No Tesla" in result.message

    def fail_ip():
        raise RuntimeError("no ip")

    monkeypatch.setattr(scan_module, "get_my_ip", fail_ip)
    with pytest.raises(ValueError, match="unable to determine"):
        setup_core.scan_gateways(SetupScanRequest())


def test_status_reports_restart_after_first_gateway_from_empty(core_env, monkeypatch):
    core_env.config_path.write_text("gateways: []\n", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(_request(expected_revision=revision))
    assert result.saved is True

    status = setup_core.get_setup_status()
    assert status.state == "restart_required"
    assert status.restart_required is True
    assert status.config_error is None
    assert status.config_file_revision != status.config_revision

    # Simulate the restart: the same file is now the loaded configuration.
    settings.gateways = [_gateway()]
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings._config_error = None
    settings._config_partial = False
    status = setup_core.get_setup_status()
    assert status.restart_required is False
    assert status.state != "restart_required"


def test_status_reports_restart_after_malformed_repair(core_env, monkeypatch):
    core_env.config_path.write_text("gateways: [\n", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = "Unable to read config file (ParserError)"
    settings._config_warnings = ["stale warning"]
    settings._config_partial = True
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(_request(expected_revision=revision))
    assert result.saved is True

    status = setup_core.get_setup_status()
    assert status.state == "restart_required"
    assert status.restart_required is True
    assert status.config_error is None
    assert status.config_partial is False
    assert "stale warning" not in status.warnings

    settings.gateways = [_gateway()]
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    status = setup_core.get_setup_status()
    assert status.restart_required is False
    assert status.state != "restart_required"


@pytest.mark.parametrize("value", [5, None, "invalid", {}])
def test_non_list_gateways_are_repaired_with_warning(core_env, value):
    core_env.config_path.write_text(
        yaml.safe_dump({"gateways": value}), encoding="utf-8"
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = "Config file has no 'gateways' list"
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(_request(expected_revision=revision))
    assert result.saved is True
    assert any("not a list" in warning for warning in result.warnings)
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    assert isinstance(doc["gateways"], list)
    assert doc["gateways"][0]["id"] == "home"


def test_valid_gateway_list_is_accepted(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "gateways": [
                    {"id": "home", "host": "192.168.91.1", "gw_pwd": "a"},
                    {"id": "cabin", "host": "192.168.91.2", "gw_pwd": "b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(_request(expected_revision=revision))
    assert result.saved is True
    assert not any("not a list" in warning for warning in result.warnings)


def test_configure_preserves_unknown_gateway_fields(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "custom_top": {"keep": True},
                "settings": {"failover": False},
                "gateways": [
                    {
                        "id": "home",
                        "host": "192.168.91.1",
                        "gw_pwd": "secret",
                        "future_field": "keep-me",
                        "custom_option": {"enabled": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    gateway = doc["gateways"][0]
    assert gateway["gw_pwd"] == "secret"
    assert gateway["future_field"] == "keep-me"
    assert gateway["custom_option"] == {"enabled": True}

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.2",
                    "gw_pwd": "new-secret",
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    gateway = doc["gateways"][0]
    assert gateway["host"] == "192.168.91.2"
    assert gateway["gw_pwd"] == "new-secret"
    assert gateway["future_field"] == "keep-me"
    assert gateway["custom_option"] == {"enabled": True}
    assert doc["custom_top"] == {"keep": True}
    assert doc["settings"]["failover"] is False


def test_unknown_gateway_fields_follow_rename(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "gateways": [
                    {
                        "id": "home",
                        "host": "192.168.91.1",
                        "gw_pwd": "secret",
                        "future_field": "keep-me",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "renamed",
                    "original_id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    gateway = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))[
        "gateways"
    ][0]
    assert gateway["id"] == "renamed"
    assert gateway["gw_pwd"] == "secret"
    assert gateway["future_field"] == "keep-me"


def test_new_gateway_does_not_inherit_unknown_fields(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "gateways": [
                    {
                        "id": "home",
                        "host": "192.168.91.1",
                        "gw_pwd": "secret",
                        "future_field": "keep-me",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                },
                {
                    "id": "new",
                    "mode": "tedapi",
                    "host": "192.168.91.3",
                    "gw_pwd": "new-secret",
                },
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    by_id = {gateway["id"]: gateway for gateway in doc["gateways"]}
    assert by_id["home"]["future_field"] == "keep-me"
    assert "future_field" not in by_id["new"]


def test_status_never_returns_bootstrap_token(core_env, monkeypatch):
    monkeypatch.setattr(setup_core, "_SETUP_TOKEN", "bootstrap-secret")
    settings.control_secret = None
    settings._config_source = "none"
    settings._config_path = None
    settings._config_error = None
    settings._config_warnings = []
    settings._config_revision = None
    settings._config_partial = False
    settings.gateways = []

    status = setup_core.get_setup_status()
    dumped = str(status.model_dump())
    assert "bootstrap-secret" not in dumped
    assert any("per process" in warning for warning in status.warnings)


@pytest.mark.parametrize("mode", ["hybrid", "hybrid_fleetapi"])
def test_hybrid_rsa_key_alone_is_rejected(core_env, mode):
    key = core_env.authpath / "hybrid-key.pem"
    key.write_text("fake", encoding="utf-8")
    item = SetupGatewayInput(
        id="home",
        mode=mode,
        host="192.168.91.1",
        rsa_key_path=str(key),
        email="owner@example.com",
        authpath=str(core_env.authpath),
    )
    issues = []
    assert setup_core._validate_gateway(item, {}, issues) is None
    assert any(
        issue.field == "password" and issue.severity == "error" for issue in issues
    )


@pytest.mark.parametrize("mode", ["hybrid", "hybrid_fleetapi"])
@pytest.mark.parametrize(
    "credential", [{"password": "customer"}, {"gw_pwd": "gateway"}]
)
def test_hybrid_rsa_with_password_is_valid_and_roundtrips(core_env, mode, credential):
    key = core_env.authpath / "hybrid-key.pem"
    key.write_text("fake", encoding="utf-8")
    values = {
        "id": "home",
        "mode": mode,
        "host": "192.168.91.1",
        "rsa_key_path": str(key),
        "email": "owner@example.com",
        "authpath": str(core_env.authpath),
    }
    values.update(credential)
    item = SetupGatewayInput(**values)
    issues = []
    config = setup_core._validate_gateway(item, {}, issues)
    assert config is not None
    assert not [issue for issue in issues if issue.severity == "error"]
    assert config.password == credential.get("password")
    assert config.gw_pwd == credential.get("gw_pwd")
    assert config.rsa_key_path == str(key)
    assert setup_core._setup_mode(config) == mode

    reloaded = GatewayConfig(**config.model_dump(exclude_none=True))
    assert setup_core._setup_mode(reloaded) == mode


def test_saved_hybrid_rsa_reopens_and_saves(core_env):
    key = core_env.authpath / "hybrid-key.pem"
    key.write_text("fake", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_revision = None
    settings._config_warnings = []
    settings._config_partial = False
    settings.gateways = []

    request = _request(
        gateways=[
            {
                "id": "home",
                "mode": "hybrid",
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": str(key),
                "email": "owner@example.com",
                "authpath": str(core_env.authpath),
            }
        ]
    )
    assert setup_core.apply_setup_request(request).saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    config = GatewayConfig(**doc["gateways"][0])
    assert setup_core._setup_mode(config) == "hybrid"

    reopened = {
        "id": "home",
        "mode": "hybrid",
        "host": "192.168.91.1",
        "rsa_key_path": str(key),
        "email": "owner@example.com",
        "authpath": str(core_env.authpath),
    }
    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(gateways=[reopened], expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    config = GatewayConfig(**doc["gateways"][0])
    assert setup_core._setup_mode(config) == "hybrid"
    assert config.password == "customer"


def test_switch_hybrid_to_local_clears_cloud_fields(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "gateways": [
                    {
                        "id": "home",
                        "host": "192.168.91.1",
                        "password": "old-password",
                        "email": "owner@example.com",
                        "authpath": str(core_env.authpath),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings._config_warnings = []
    settings._config_partial = False
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "home",
                    "mode": "local",
                    "host": "192.168.91.1",
                    "password": "new-password",
                    "email": "owner@example.com",
                    "authpath": str(core_env.authpath),
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    gateway = doc["gateways"][0]
    assert "email" not in gateway
    assert "authpath" not in gateway
    assert gateway["password"] == "new-password"
    config = GatewayConfig(**gateway)
    assert setup_core._setup_mode(config) == "local"


def test_hybrid_config_keeps_cloud_fields(core_env):
    core_env.config_path.write_text(
        yaml.safe_dump(
            {
                "gateways": [
                    {
                        "id": "home",
                        "host": "192.168.91.1",
                        "password": "customer",
                        "email": "owner@example.com",
                        "authpath": str(core_env.authpath),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_revision = setup_core._revision(core_env.config_path.read_bytes())
    settings._config_warnings = []
    settings._config_partial = False
    settings.gateways = []

    revision = setup_core._revision(core_env.config_path.read_bytes())
    result = setup_core.apply_setup_request(
        _request(
            gateways=[
                {
                    "id": "home",
                    "mode": "hybrid",
                    "host": "192.168.91.1",
                    "password": "customer",
                    "email": "owner@example.com",
                    "authpath": str(core_env.authpath),
                }
            ],
            expected_revision=revision,
        )
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    gateway = doc["gateways"][0]
    assert gateway["email"] == "owner@example.com"
    assert gateway["authpath"] == str(core_env.authpath)
    config = GatewayConfig(**gateway)
    assert setup_core._setup_mode(config) == "hybrid"


@pytest.mark.parametrize(
    "gateway,expected_setup,expected_derive,expected_cloud",
    [
        (
            {
                "mode": "local",
                "host": "192.168.91.1",
                "password": "customer",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "local",
            "Local/Basic LAN",
            False,
        ),
        (
            {
                "mode": "basic_lan",
                "host": "192.168.91.1",
                "password": "customer",
            },
            "local",
            "Local/Basic LAN",
            False,
        ),
        (
            {
                "mode": "basic_lan",
                "host": "192.168.91.1",
                "password": "customer",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid",
            "Hybrid",
            True,
        ),
        (
            {
                "mode": "tedapi",
                "host": "192.168.91.1",
                "gw_pwd": "gateway",
            },
            "tedapi",
            "TEDAPI",
            False,
        ),
        (
            {
                "mode": "tedapi",
                "host": "192.168.91.1",
                "gw_pwd": "gateway",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid",
            "Hybrid",
            True,
        ),
        (
            {
                "mode": "tedapi_v1r",
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
            },
            "tedapi_v1r",
            "TEDAPI v1r",
            False,
        ),
        (
            {
                "mode": "tedapi_v1r",
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid",
            "Hybrid",
            True,
        ),
        (
            {
                "mode": "hybrid",
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid",
            "Hybrid",
            True,
        ),
        (
            {
                "mode": "hybrid",
                "host": "192.168.91.1",
                "gw_pwd": "gateway",
                "rsa_key_path": "KEY",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid",
            "Hybrid",
            True,
        ),
        (
            {
                "mode": "hybrid_fleetapi",
                "host": "192.168.91.1",
                "password": "customer",
                "rsa_key_path": "KEY",
                "email": "owner@example.com",
                "authpath": "AUTHPATH",
            },
            "hybrid_fleetapi",
            "Hybrid FleetAPI",
            True,
        ),
    ],
)
def test_mode_classification_after_yaml_reload(
    core_env, gateway, expected_setup, expected_derive, expected_cloud
):
    key = core_env.authpath / "matrix-key.pem"
    key.write_text("fake", encoding="utf-8")
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)
    settings._config_error = None
    settings._config_warnings = []
    settings._config_partial = False
    settings.gateways = []

    values = {"id": "home"}
    values.update(gateway)
    for name, value in list(values.items()):
        if value == "KEY":
            values[name] = str(key)
        elif value == "AUTHPATH":
            values[name] = str(core_env.authpath)
    revision = (
        setup_core._revision(core_env.config_path.read_bytes())
        if core_env.config_path.exists()
        else None
    )
    result = setup_core.apply_setup_request(
        _request(gateways=[values], expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(core_env.config_path.read_text(encoding="utf-8"))
    persisted = GatewayConfig(**doc["gateways"][0])
    assert setup_core._setup_mode(persisted) == expected_setup
    assert setup_core._derive_mode(persisted) == expected_derive
    assert bool(persisted.email and persisted.authpath) is expected_cloud


def test_rsa_hybrid_status_modes_are_consistent(core_env):
    from app.core.gateway_manager import gateway_manager

    gateway_manager.cache.pop("home", None)
    settings.gateways = [
        GatewayConfig(
            id="home",
            host="192.168.91.1",
            password="customer",
            rsa_key_path=str(core_env.authpath / "hybrid-key.pem"),
            email="owner@example.com",
            authpath=str(core_env.authpath),
        )
    ]
    settings._config_source = "default_file"
    settings._config_path = str(core_env.config_path)

    status = setup_core.get_setup_status()
    assert status.gateway_configs[0]["mode"] == "hybrid"
    assert status.gateways[0].mode == "Hybrid"
