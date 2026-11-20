"""v1r registration and status edge paths without Tesla credentials."""

import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.core import setup as setup_core
from app.models.setup import (
    SetupCloudAuthCompleteRequest,
    SetupV1rRegisterRequest,
    SetupV1rSite,
    SetupV1rStatusRequest,
)


@pytest.fixture
def v1r_env(monkeypatch, tmp_path):
    authpath = tmp_path / "auth"
    authpath.mkdir()
    old_gateways = settings.gateways
    old_control = settings.control_secret
    old_private = dict(settings.__pydantic_private__)
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
    import pypowerwall.v1r_register as register_module

    monkeypatch.setattr(
        register_module, "generate_rsa_key", lambda path: (None, b"public-der")
    )
    monkeypatch.setattr(
        register_module, "_check_key_state", lambda response, pubkey_der=None: 1
    )
    try:
        yield authpath
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)


def _auth(root, expires_at=None):
    (root / ".pypowerwall.auth").write_text(
        json.dumps(
            {
                "owner@example.com": {
                    "sso": {
                        "access_token": "owner-token",
                        "refresh_token": "owner-refresh",
                        "expires_at": int(
                            time.time() + 3600 if expires_at is None else expires_at
                        ),
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _fleet(root, **overrides):
    config = {
        "CLIENT_ID": "client-id",
        "CLIENT_SECRET": "client-secret",
        "DOMAIN": "example.com",
        "REDIRECT_URI": "https://example.com/access",
        "AUDIENCE": "https://fleet-api.example.com",
        "access_token": "fleet-at",
        "refresh_token": "fleet-rt",
        "site_id": None,
    }
    config.update(overrides)
    (root / ".pypowerwall.fleetapi").write_text(json.dumps(config), encoding="utf-8")
    return config


def _site(siteid="123"):
    return SetupV1rSite(siteid=siteid, name="Home")


def test_v1r_sites_no_sites_response(v1r_env, monkeypatch):
    import pypowerwall.v1r_register as register_module

    monkeypatch.setattr(
        register_module,
        "api_call",
        lambda *args, **kwargs: (200, {"response": []}),
    )
    with pytest.raises(ValueError, match="no Tesla energy sites"):
        setup_core._v1r_sites("token")


def test_v1r_path_rejects_file_path(v1r_env):
    with pytest.raises(ValueError, match="not a directory"):
        setup_core._v1r_path(str(Path.cwd() / "README.md"))


def test_v1r_auth_file_skips_non_dict_accounts(v1r_env):
    result = setup_core.v1r_auth_file(
        setup_core.SetupV1rAuthFileRequest(
            authpath=str(v1r_env),
            content=json.dumps(
                {
                    "bad@example.com": "not-a-dict",
                    "owner@example.com": {"sso": {"access_token": "token"}},
                }
            ),
        )
    )
    assert result.email == "owner@example.com"


def test_v1r_register_selected_site_not_available(v1r_env, monkeypatch):
    _auth(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("token", "owner@example.com"),
    )
    monkeypatch.setattr(setup_core, "_v1r_sites", lambda token: [_site("123")])
    with pytest.raises(ValueError, match="selected site"):
        setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(v1r_env), siteid="999")
        )


def test_v1r_register_retries_rejected_site_lookup(v1r_env, monkeypatch):
    _auth(v1r_env)
    calls = []
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("old-token", "owner@example.com"),
    )
    monkeypatch.setattr(
        setup_core,
        "_v1r_refresh_auth",
        lambda root, email=None: ("new-token", "owner@example.com"),
    )
    monkeypatch.setattr(
        setup_core,
        "_v1r_sites",
        lambda token: (
            (_ for _ in ()).throw(ValueError("401 rejected"))
            if token == "old-token"
            else [_site("123")]
        ),
    )
    import pypowerwall.v1r_register as register_module

    def fake_api(*args, **kwargs):
        calls.append(kwargs.get("token"))
        return 200, {"ok": True}

    monkeypatch.setattr(register_module, "api_call", fake_api)
    result = setup_core.v1r_register(
        SetupV1rRegisterRequest(authpath=str(v1r_env), siteid="123")
    )
    assert result.siteid == "123"
    assert calls == ["new-token"]


def test_v1r_register_retries_post_401_once(v1r_env, monkeypatch):
    _auth(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("old-token", "owner@example.com"),
    )
    monkeypatch.setattr(setup_core, "_v1r_sites", lambda token: [_site("123")])
    monkeypatch.setattr(
        setup_core,
        "_v1r_refresh_auth",
        lambda root, email=None: ("new-token", "owner@example.com"),
    )
    import pypowerwall.v1r_register as register_module

    calls = []
    responses = [(401, {}), (200, {"ok": True})]

    def fake_api(*args, **kwargs):
        calls.append(kwargs.get("token"))
        return responses.pop(0)

    monkeypatch.setattr(register_module, "api_call", fake_api)
    result = setup_core.v1r_register(
        SetupV1rRegisterRequest(authpath=str(v1r_env), siteid="123")
    )
    assert result.siteid == "123"
    assert calls == ["old-token", "new-token"]


def test_v1r_register_rejects_double_401_and_non_200(v1r_env, monkeypatch):
    _auth(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("token", "owner@example.com"),
    )
    monkeypatch.setattr(setup_core, "_v1r_sites", lambda token: [_site("123")])
    monkeypatch.setattr(
        setup_core,
        "_v1r_refresh_auth",
        lambda root, email=None: ("new-token", "owner@example.com"),
    )
    import pypowerwall.v1r_register as register_module

    monkeypatch.setattr(register_module, "api_call", lambda *a, **k: (403, {}))
    with pytest.raises(ValueError, match="rejected the access token"):
        setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(v1r_env), siteid="123")
        )
    monkeypatch.setattr(register_module, "api_call", lambda *a, **k: (500, {}))
    with pytest.raises(ValueError, match="registration failed"):
        setup_core.v1r_register(
            SetupV1rRegisterRequest(authpath=str(v1r_env), siteid="123")
        )


def test_v1r_register_owner_requires_site_selection(v1r_env, monkeypatch):
    _auth(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("token", "owner@example.com"),
    )
    monkeypatch.setattr(
        setup_core,
        "_v1r_sites",
        lambda token: [_site("123"), _site("456")],
    )
    result = setup_core.v1r_register(SetupV1rRegisterRequest(authpath=str(v1r_env)))
    assert result.needs_site is True
    assert [site.siteid for site in result.sites] == ["123", "456"]


def test_v1r_register_fleet_requires_site_selection(v1r_env, monkeypatch):
    _fleet(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites_with_refresh",
        lambda root, config: [_site("123"), _site("456")],
    )
    result = setup_core.v1r_register(
        SetupV1rRegisterRequest(authpath=str(v1r_env), method="fleetapi")
    )
    assert result.needs_site is True
    assert len(result.sites) == 2


def test_v1r_register_fleet_refreshes_and_selects_single_site(v1r_env, monkeypatch):
    _fleet(v1r_env, access_token="", refresh_token="refresh")
    calls = []
    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, config: {**config, "access_token": "refreshed"},
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites_with_refresh",
        lambda root, config: [_site("123")],
    )

    def fake_command(root, siteid, payload):
        calls.append((root, siteid))
        return {"ok": True}

    monkeypatch.setattr(setup_core, "_fleet_v1r_command", fake_command)
    result = setup_core.v1r_register(
        SetupV1rRegisterRequest(authpath=str(v1r_env), method="fleetapi")
    )
    assert result.siteid == "123"
    assert calls == [(v1r_env, "123")]


def test_v1r_status_rejects_invalid_siteid(v1r_env):
    with pytest.raises(ValueError, match="numeric"):
        setup_core.v1r_registration_status(
            SetupV1rStatusRequest(authpath=str(v1r_env), siteid="invalid")
        )


def test_v1r_status_fleet_mode(v1r_env, monkeypatch):
    monkeypatch.setattr(
        setup_core, "_fleet_v1r_command", lambda root, siteid, payload: {"ok": True}
    )
    result = setup_core.v1r_registration_status(
        SetupV1rStatusRequest(authpath=str(v1r_env), siteid="123", method="fleetapi")
    )
    assert result.siteid == "123"
    assert result.verified is False


def test_v1r_status_refreshes_and_handles_errors(v1r_env, monkeypatch):
    _auth(v1r_env)
    monkeypatch.setattr(
        setup_core,
        "_v1r_auth_token",
        lambda root, email=None: ("old-token", "owner@example.com"),
    )
    monkeypatch.setattr(
        setup_core,
        "_v1r_refresh_auth",
        lambda root, email=None: ("new-token", "owner@example.com"),
    )
    import pypowerwall.v1r_register as register_module

    responses = [(401, {}), (200, {"ok": True})]
    monkeypatch.setattr(register_module, "api_call", lambda *a, **k: responses.pop(0))
    result = setup_core.v1r_registration_status(
        SetupV1rStatusRequest(authpath=str(v1r_env), siteid="123")
    )
    assert result.siteid == "123"

    monkeypatch.setattr(register_module, "api_call", lambda *a, **k: (403, {}))
    with pytest.raises(ValueError, match="rejected the access token"):
        setup_core.v1r_registration_status(
            SetupV1rStatusRequest(authpath=str(v1r_env), siteid="123")
        )
    monkeypatch.setattr(register_module, "api_call", lambda *a, **k: (500, {}))
    with pytest.raises(ValueError, match="status check failed"):
        setup_core.v1r_registration_status(
            SetupV1rStatusRequest(authpath=str(v1r_env), siteid="123")
        )


def test_cloud_complete_uses_explicit_valid_site(v1r_env, monkeypatch):
    setup_core._CLOUD_AUTH_STATES["state"] = {
        "verifier": "v",
        "email": "owner@example.com",
        "authpath": str(v1r_env),
        "region": "us",
        "expires": time.monotonic() + 600,
    }
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth,
        "_exchange_code",
        lambda *args, **kwargs: {"access_token": "at", "refresh_token": "rt"},
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 123}, {"energy_site_id": 456}],
    )
    result = setup_core.cloud_auth_complete(
        SetupCloudAuthCompleteRequest(
            callback_url="https://x/access?code=abc&state=state",
            siteid="456",
        )
    )
    assert result.siteid == "456"
    assert result.needs_site is False
