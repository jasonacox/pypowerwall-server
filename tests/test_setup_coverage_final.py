"""Final small-branch tests for setup service cleanup and repair paths."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import settings
from app.core import setup as setup_core
from app.models.setup import (
    SetupAuthFileRequest,
    SetupConfigureRequest,
    SetupEnvImportRequest,
    SetupGatewayInput,
    SetupV1rRegisterRequest,
)


@pytest.fixture
def final_env(monkeypatch, tmp_path):
    authpath = tmp_path / "auth"
    authpath.mkdir()
    config_path = tmp_path / "gateways.yaml"
    old_gateways = settings.gateways
    old_control = settings.control_secret
    old_private = dict(settings.__pydantic_private__)
    monkeypatch.setattr(settings, "gateways", [])
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(settings, "pw_authpath", str(authpath), raising=False)
    monkeypatch.setattr(setup_core, "default_config_path", lambda: str(config_path))
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
    try:
        yield SimpleNamespace(authpath=authpath, config_path=config_path)
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)


def _gateway_doc(**overrides):
    values = {
        "id": "home",
        "mode": "tedapi",
        "host": "192.168.91.1",
        "gw_pwd": "secret",
    }
    values.update(overrides)
    return values


def test_setup_auth_token_generation_and_missing_expected(final_env, monkeypatch):
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(setup_core, "_SETUP_TOKEN", None)
    first = setup_core.setup_auth_token()
    assert first
    assert setup_core.setup_auth_token() == first
    monkeypatch.setattr(setup_core, "setup_auth_token", lambda: None)
    assert setup_core.verify_setup_token("anything") is False


def test_write_atomic_cleans_temporary_file_on_replace_failure(final_env, monkeypatch):
    def fail_replace(*args):
        raise OSError("cannot replace")

    monkeypatch.setattr(setup_core.os, "replace", fail_replace)
    with pytest.raises(OSError):
        setup_core._write_atomic(str(final_env.config_path), {"gateways": []})
    assert list(final_env.config_path.parent.glob(".*.tmp")) == []


def test_apply_setup_request_surfaces_validation_errors(final_env):
    with pytest.raises(ValueError, match="Host is required"):
        setup_core.apply_setup_request(
            SetupConfigureRequest(
                gateways=[
                    {
                        "id": "home",
                        "mode": "tedapi",
                        "gw_pwd": "secret",
                    }
                ]
            )
        )


def test_fleet_test_connection_uses_single_refreshed_site(final_env, monkeypatch):
    (final_env.authpath / ".pypowerwall.fleetapi").write_text(
        json.dumps({"site_id": None}), encoding="utf-8"
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites_with_refresh",
        lambda path, config: [SimpleNamespace(siteid="123")],
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda path, siteid: {"site_name": "Home"},
    )
    result = setup_core.test_gateway_connection(
        SetupGatewayInput(
            id="fleet",
            mode="fleetapi",
            email="owner@example.com",
            authpath=str(final_env.authpath),
        )
    )
    assert result.ok is True
    assert result.site_name == "Home"


def test_v1r_lookup_non_auth_error_is_not_retried(final_env, monkeypatch):
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("token", "owner@example.com"),
    )
    monkeypatch.setattr(
        setup_core,
        "_v1r_sites",
        lambda token: (_ for _ in ()).throw(ValueError("network timeout")),
    )
    with pytest.raises(ValueError, match="network timeout"):
        setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(final_env.authpath))
        )


def test_fleet_v1r_command_refreshes_missing_access_token(final_env, monkeypatch):
    (final_env.authpath / ".pypowerwall.fleetapi").write_text(
        json.dumps(
            {
                "CLIENT_ID": "client",
                "AUDIENCE": "https://api.example.com",
                "access_token": "",
                "refresh_token": "refresh",
            }
        ),
        encoding="utf-8",
    )
    from pypowerwall.fleetapi import fleetapi as fleet_module

    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, config: {
            **config,
            "access_token": "new",
            "refresh_token": "refresh",
        },
    )
    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200, json=lambda: {"ok": True}
        ),
    )
    assert setup_core._fleet_v1r_command(final_env.authpath, "123", {}) == {"ok": True}


def test_env_preview_without_target_file(final_env):
    preview = setup_core.env_import_preview()
    assert preview["target_path"] == str(final_env.config_path)
    assert preview["revision"] is None


def test_env_activate_missing_file_with_expected_revision(final_env):
    with pytest.raises(ValueError, match="changed"):
        setup_core.env_import_activate(SetupEnvImportRequest(expected_revision="stale"))


def test_env_activate_repairs_malformed_file_with_config_error(final_env, monkeypatch):
    final_env.config_path.write_text("gateways: [", encoding="utf-8")
    settings._config_error = "invalid"
    revision = setup_core._revision(final_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    assert yaml.safe_load(final_env.config_path.read_text())["gateways"] == []


def test_env_activate_merges_non_mapping_server_and_settings(final_env, monkeypatch):
    final_env.config_path.write_text(
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
    revision = setup_core._revision(final_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(final_env.config_path.read_text())
    assert doc["server"]["host"] == "127.0.0.1"
    assert doc["settings"]["failover"] is False


def test_fleet_write_cleanup_tolerates_unlink_failure(final_env, monkeypatch):
    def fail_replace(*args):
        raise OSError("replace failed")

    def fail_unlink(*args):
        raise OSError("unlink failed")

    monkeypatch.setattr(setup_core.os, "replace", fail_replace)
    monkeypatch.setattr(setup_core.os, "unlink", fail_unlink)
    with pytest.raises(OSError, match="replace failed"):
        setup_core._fleet_write_config(final_env.authpath, {"a": 1})


def test_fleet_key_ignores_public_chmod_failure(final_env, monkeypatch):
    original_chmod = setup_core.os.chmod

    def flaky_chmod(path, mode):
        if str(path).endswith("com.tesla.3p.public-key.pem"):
            raise OSError("chmod failed")
        return original_chmod(path, mode)

    monkeypatch.setattr(setup_core.os, "chmod", flaky_chmod)
    result = setup_core.fleet_generate_key(
        setup_core.SetupFleetKeyRequest(authpath=str(final_env.authpath))
    )
    assert result.created is True
    assert Path(result.public_key_path).is_file()


def test_write_private_text_cleanup_tolerates_unlink_failure(final_env, monkeypatch):
    def fail_replace(*args):
        raise OSError("replace failed")

    def fail_unlink(*args):
        raise OSError("unlink failed")

    monkeypatch.setattr(setup_core.os, "replace", fail_replace)
    monkeypatch.setattr(setup_core.os, "unlink", fail_unlink)
    with pytest.raises(OSError, match="replace failed"):
        setup_core._write_private_text(final_env.authpath / "file", "content")


def test_backup_file_ignores_chmod_failure(final_env, monkeypatch):
    path = final_env.authpath / "auth.json"
    path.write_text("{}", encoding="utf-8")

    original_chmod = setup_core.os.chmod

    def fail_chmod(*args, **kwargs):
        if kwargs:
            return original_chmod(*args, **kwargs)
        raise OSError("chmod failed")

    monkeypatch.setattr(setup_core.os, "chmod", fail_chmod)
    backup = setup_core._backup_file(path)
    assert backup == str(path) + ".bak"
    assert Path(backup).is_file()


def test_merge_auth_file_replaces_non_mapping_existing(final_env):
    path = final_env.authpath / ".pypowerwall.auth"
    path.write_text("[]", encoding="utf-8")
    merged = setup_core._merge_auth_file(path, {"owner@example.com": {}})
    assert merged == {"owner@example.com": {}}


def test_import_cloud_auth_file_skips_non_mapping_account(final_env):
    result = setup_core.import_auth_file(
        SetupAuthFileRequest(
            mode="cloud",
            authpath=str(final_env.authpath),
            content=json.dumps(
                {
                    "bad@example.com": "not-a-mapping",
                    "owner@example.com": {"sso": {"access_token": "token"}},
                }
            ),
        )
    )
    assert result.email == "owner@example.com"


def test_import_fleet_config_without_siteid(final_env):
    result = setup_core.import_auth_file(
        SetupAuthFileRequest(
            mode="fleetapi",
            authpath=str(final_env.authpath),
            content=json.dumps(
                {
                    "CLIENT_ID": "client",
                    "CLIENT_SECRET": "secret",
                    "DOMAIN": "example.com",
                    "AUDIENCE": "https://api.example.com",
                    "access_token": "token",
                }
            ),
        )
    )
    assert result.siteid is None
    assert "fleetapi" in result.files


def test_cloud_auth_start_keeps_unexpired_pending_state(final_env, monkeypatch):
    from pypowerwall import tesla_auth

    setup_core._CLOUD_AUTH_STATES["live"] = {"expires": time.monotonic() + 600}
    monkeypatch.setattr(
        tesla_auth,
        "_build_auth_url",
        lambda region="us": ("https://auth.example", "verifier", "state-new"),
    )
    setup_core.cloud_auth_start("owner@example.com", str(final_env.authpath))
    assert "live" in setup_core._CLOUD_AUTH_STATES
    assert "state-new" in setup_core._CLOUD_AUTH_STATES


def test_docker_preflight_reports_route_table_and_unwritable_config(
    final_env, monkeypatch
):
    monkeypatch.setattr(setup_core, "_is_writable", lambda path: False)
    original_read_text = Path.read_text

    def read_text(self, encoding=None, errors=None):
        if self.as_posix() == "/proc/net/route":
            return "Iface Destination Gateway"
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    result = setup_core.docker_preflight()
    assert any(
        check["key"] == "network" and check["status"] == "success"
        for check in result["checks"]
    )
    assert any("not writable" in warning for warning in result["warnings"])


def test_validate_path_skips_unresolvable_allowed_root(final_env, monkeypatch):
    original_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        if self.as_posix() == "/auth":
            raise OSError("unresolvable root")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert setup_core._path_allowed("/auth/owner.json") is False


def test_v1r_test_connection_requires_item_secret(final_env, monkeypatch):
    key = final_env.authpath / "tedapi.pem"
    key.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.91.1", 443))],
    )

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        setup_core.socket, "create_connection", lambda *args, **kwargs: FakeSocket()
    )
    item = SetupGatewayInput(
        id="v1r",
        mode="tedapi_v1r",
        host="192.168.91.1",
        gw_pwd="placeholder",
        rsa_key_path=str(key),
    ).model_copy(update={"gw_pwd": None, "password": None})
    result = setup_core.test_gateway_connection(item, {"gw_pwd": "existing-secret"})
    assert result.ok is False
    assert "connection test failed" in result.steps[-1].detail


def test_docker_preflight_warns_about_container_networking(final_env, monkeypatch):
    original_exists = Path.exists

    def exists(self):
        if self.as_posix() == "/.dockerenv":
            return True
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", exists)
    result = setup_core.docker_preflight()
    assert any("host networking" in warning for warning in result["warnings"])


def test_initialize_setup_auth_keeps_existing_bootstrap_token(final_env, monkeypatch):
    monkeypatch.setattr(settings, "control_secret", None)
    monkeypatch.setattr(setup_core, "_SETUP_TOKEN", "existing-token")
    setup_core.initialize_setup_auth()
    assert setup_core._SETUP_TOKEN == "existing-token"


def test_status_skips_non_scalar_setting_values(final_env, monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", {"not": "scalar"})
    settings.gateways = []
    settings._config_source = "none"
    status = setup_core.get_setup_status()
    assert "cors_origins" not in status.settings


def test_status_ignores_non_mapping_settings_block(final_env):
    final_env.config_path.write_text(
        "settings: not-a-mapping\ngateways: []\n", encoding="utf-8"
    )
    settings._config_source = "default_file"
    settings._config_path = str(final_env.config_path)
    settings._config_error = None
    status = setup_core.get_setup_status()
    assert status.settings_keys == []


def test_mqtt_controls_with_control_secret_has_no_warning(final_env):
    result, _, _ = setup_core.validate_setup_request(
        SetupConfigureRequest(
            gateways=[
                {
                    "id": "home",
                    "mode": "tedapi",
                    "host": "192.168.91.1",
                    "gw_pwd": "secret",
                }
            ],
            options={"mqtt_controls": 1, "control_secret": "admin-token"},
        ),
        {},
    )
    assert result.valid is True
    assert not any(issue.field == "options.mqtt_controls" for issue in result.warnings)


def test_docker_preflight_writable_data_without_config_path(final_env, monkeypatch):
    monkeypatch.setattr(
        setup_core.Path, "is_dir", lambda self: self.as_posix() == "/data"
    )
    monkeypatch.setattr(setup_core, "_effective_path", lambda: None)
    monkeypatch.setattr(setup_core.os, "access", lambda path, mode: True)
    result = setup_core.docker_preflight()
    assert result["checks"][0]["status"] == "success"
    assert not any("not writable" in warning for warning in result["warnings"])
    assert not any(check["key"] == "config" for check in result["checks"])


def test_env_import_without_writable_options(final_env, monkeypatch):
    monkeypatch.setattr(setup_core, "_WRITABLE_OPTION_FIELDS", set())
    settings.gateways = []
    settings._config_source = "none"
    preview = setup_core.env_import_preview()
    assert "settings" not in preview["yaml"]
    result = setup_core.env_import_activate(SetupEnvImportRequest())
    assert result.saved is True
    assert "settings" not in yaml.safe_load(
        final_env.config_path.read_text(encoding="utf-8")
    )


def test_env_activate_merges_existing_mapping_blocks(final_env, monkeypatch):
    final_env.config_path.write_text(
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
    revision = setup_core._revision(final_env.config_path.read_bytes())
    result = setup_core.env_import_activate(
        SetupEnvImportRequest(expected_revision=revision)
    )
    assert result.saved is True
    doc = yaml.safe_load(final_env.config_path.read_text(encoding="utf-8"))
    assert doc["server"]["host"] == "new"
    assert doc["settings"]["failover"] is False
