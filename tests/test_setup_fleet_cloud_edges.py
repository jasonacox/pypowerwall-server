"""FleetAPI, Cloud, v1r, env, and import edge-case coverage."""

import json
import socket
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.config import settings
from app.core import setup as setup_core
from app.models.setup import (
    SetupAuthFileRequest,
    SetupAuthTokensRequest,
    SetupCloudAuthCompleteRequest,
    SetupCloudAuthUseRequest,
    SetupFleetCompleteRequest,
    SetupFleetKeyRequest,
    SetupFleetSiteRequest,
    SetupFleetStartRequest,
)


@pytest.fixture
def edge_env(monkeypatch, tmp_path):
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
    try:
        yield authpath
    finally:
        settings.gateways = old_gateways
        settings.control_secret = old_control
        settings.__pydantic_private__.update(old_private)


def _write_fleet(root, **overrides):
    config = {
        "CLIENT_ID": "client-id",
        "CLIENT_SECRET": "client-secret",
        "DOMAIN": "example.com",
        "REDIRECT_URI": "https://example.com/access",
        "AUDIENCE": "https://fleet-api.example.com",
        "access_token": "fleet-at",
        "refresh_token": "fleet-rt",
        "site_id": 123,
    }
    config.update(overrides)
    (root / ".pypowerwall.fleetapi").write_text(json.dumps(config), encoding="utf-8")
    return config


def _write_auth(root, expires_at=None):
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


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Opened:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Opener:
    def __init__(self, result):
        self.result = result

    def open(self, url, timeout=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_public_domain_validation_and_resolution(monkeypatch):
    with pytest.raises(ValueError, match="public hostname"):
        setup_core._fleet_public_domain("https://example.com")
    with pytest.raises(ValueError, match="port"):
        setup_core._fleet_public_domain("example.com:443")
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    assert setup_core._fleet_public_domain("Example.COM.") == "example.com"
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("192.168.1.2", 443))],
    )
    with pytest.raises(ValueError, match="public address"):
        setup_core._fleet_public_domain("example.com")
    monkeypatch.setattr(
        setup_core.socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(socket.gaierror("missing")),
    )
    with pytest.raises(ValueError, match="public address"):
        setup_core._fleet_public_domain("example.com")


def test_redirect_uri_validation():
    assert setup_core._fleet_redirect_uri("example.com", None) == (
        "https://example.com/access"
    )
    assert setup_core._fleet_redirect_uri("example.com", " https://x.example/cb ") == (
        "https://x.example/cb"
    )
    with pytest.raises(ValueError, match="absolute https"):
        setup_core._fleet_redirect_uri("example.com", "http://x.example/cb")
    with pytest.raises(ValueError, match="absolute https"):
        setup_core._fleet_redirect_uri("example.com", "not-a-url")


def test_fleet_read_config_errors_and_success(edge_env):
    with pytest.raises(ValueError, match="does not exist"):
        setup_core._fleet_read_config(edge_env)
    (edge_env / ".pypowerwall.fleetapi").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="unable to read"):
        setup_core._fleet_read_config(edge_env)
    (edge_env / ".pypowerwall.fleetapi").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        setup_core._fleet_read_config(edge_env)
    _write_fleet(edge_env)
    assert setup_core._fleet_read_config(edge_env)["site_id"] == 123


def test_fleet_write_config_cleans_temp_file_on_failure(edge_env, monkeypatch):
    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(setup_core.os, "replace", fail_replace)
    with pytest.raises(OSError):
        setup_core._fleet_write_config(edge_env, {"CLIENT_ID": "x"})
    assert list(edge_env.glob(".pypowerwall.fleetapi.*.tmp")) == []


def test_fleet_response_json_branches():
    assert setup_core._fleet_response_json(None) is None
    assert setup_core._fleet_response_json(_Response(200)) is None
    assert setup_core._fleet_response_json(_Response(200, {"a": 1})) == {"a": 1}


def test_fleet_sites_errors_and_filtering(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    with pytest.raises(ValueError, match="tokens are missing"):
        setup_core._fleet_sites({})

    for response, match in [
        (None, "no response"),
        (_Response(401), "rejected"),
        (_Response(500), "failed"),
        (_Response(200, []), "no Tesla energy sites"),
    ]:
        monkeypatch.setattr(
            fleet_module, "_http2_request", lambda *args, **kwargs: response
        )
        with pytest.raises(ValueError, match=match):
            setup_core._fleet_sites(
                {"access_token": "at", "AUDIENCE": "https://api.example.com"}
            )

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200,
            {
                "response": [
                    "bad",
                    {"site_name": "no-id"},
                    {"energy_site_id": 123, "site_name": "Home"},
                ]
            },
        ),
    )
    sites = setup_core._fleet_sites(
        {"access_token": "at", "AUDIENCE": "https://api.example.com"}
    )
    assert [site.siteid for site in sites] == ["123"]


def test_fleet_fetch_public_key_statuses(monkeypatch):
    handlers = {}
    opener = _Opener(_Opened(status=200))

    def fake_build(handler):
        handlers["handler"] = handler
        return opener

    monkeypatch.setattr(setup_core.urllib.request, "build_opener", fake_build)
    assert setup_core._fleet_fetch_public_key("https://example.com/key") == 200
    assert handlers["handler"]().redirect_request(None, None, 302, "", {}, "x") is None

    opener.result = urllib.error.HTTPError(
        "https://example.com/key", 404, "not found", None, None
    )
    assert setup_core._fleet_fetch_public_key("https://example.com/key") == 404

    opener.result = RuntimeError("offline")
    assert setup_core._fleet_fetch_public_key("https://example.com/key") is None


def test_fleet_sites_with_refresh(edge_env, monkeypatch):
    config = {"access_token": "old", "refresh_token": "rt"}
    results = [ValueError("401 rejected"), [SimpleNamespace(siteid="123")]]
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda value: (
            (_ for _ in ()).throw(results.pop(0))
            if isinstance(results[0], Exception)
            else results.pop(0)
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, value: {"access_token": "new", "refresh_token": "rt"},
    )
    refreshed = setup_core._fleet_sites_with_refresh(edge_env, config)
    assert refreshed is not None

    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda value: (_ for _ in ()).throw(ValueError("network down")),
    )
    with pytest.raises(ValueError, match="network down"):
        setup_core._fleet_sites_with_refresh(
            edge_env, {"access_token": "old", "refresh_token": "rt"}
        )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda value: (_ for _ in ()).throw(ValueError("401 rejected")),
    )
    with pytest.raises(ValueError, match="401"):
        setup_core._fleet_sites_with_refresh(edge_env, {"access_token": "old"})


def test_fleet_live_check_branches(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    _write_fleet(edge_env)
    with pytest.raises(ValueError, match="numeric"):
        setup_core._fleet_live_check(edge_env, "not-a-site")

    class FakeFleet:
        def __init__(self, configfile, site_id):
            self.site_id = site_id

        def get_site_info(self, force=False):
            return {"site_name": "Home"}

        def get_live_status(self, force=False):
            return {"percentage_charged": 50, "grid_status": "UP"}

    monkeypatch.setattr(fleet_module, "FleetAPI", FakeFleet)
    assert setup_core._fleet_live_check(edge_env, "123") == {
        "site_name": "Home",
        "battery_percent": 50,
        "grid_status": "UP",
    }

    class EmptyInfo(FakeFleet):
        def get_site_info(self, force=False):
            return {}

    monkeypatch.setattr(fleet_module, "FleetAPI", EmptyInfo)
    with pytest.raises(ValueError, match="site info"):
        setup_core._fleet_live_check(edge_env, "123")

    class EmptyLive(FakeFleet):
        def get_live_status(self, force=False):
            return {}

    monkeypatch.setattr(fleet_module, "FleetAPI", EmptyLive)
    with pytest.raises(ValueError, match="live status"):
        setup_core._fleet_live_check(edge_env, "123")


def _start_env(monkeypatch, edge_env, token_response=None, partner_response=None):
    monkeypatch.setattr(setup_core, "_fleet_public_domain", lambda domain: domain)
    monkeypatch.setattr(
        setup_core,
        "_fleet_redirect_uri",
        lambda domain, uri: uri or "https://example.com/access",
    )
    monkeypatch.setattr(setup_core, "_fleet_fetch_public_key", lambda url: 200)

    from pypowerwall.fleetapi import fleetapi as fleet_module

    calls = []
    responses = [
        token_response or _Response(200, {"access_token": "partner-token"}),
        partner_response or _Response(200, {"response": {"domain": "example.com"}}),
    ]

    def fake_http(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(fleet_module, "_http2_request", fake_http)
    return calls


def _start_request(root, **overrides):
    values = {
        "authpath": str(root),
        "client_id": "client-id",
        "client_secret": "client-secret",
        "domain": "example.com",
    }
    values.update(overrides)
    return SetupFleetStartRequest(**values)


def test_fleet_start_requires_credentials(edge_env, monkeypatch):
    monkeypatch.setattr(setup_core, "_fleet_public_domain", lambda domain: domain)
    monkeypatch.setattr(
        setup_core,
        "_fleet_redirect_uri",
        lambda domain, uri: "https://example.com/access",
    )
    with pytest.raises(ValueError, match="Client ID"):
        setup_core.fleet_setup_start(
            _start_request(edge_env, client_id="", client_secret="")
        )


def test_fleet_start_rejects_unknown_region(edge_env, monkeypatch):
    monkeypatch.setattr(setup_core, "_fleet_public_domain", lambda domain: domain)
    monkeypatch.setattr(
        setup_core,
        "_fleet_redirect_uri",
        lambda domain, uri: "https://example.com/access",
    )
    with pytest.raises(ValueError, match="region"):
        setup_core.fleet_setup_start(_start_request(edge_env, region="xx"))


def test_fleet_start_partner_token_failure(edge_env, monkeypatch):
    _start_env(monkeypatch, edge_env, token_response=_Response(500))
    with pytest.raises(ValueError, match="partner token"):
        setup_core.fleet_setup_start(_start_request(edge_env))


def test_fleet_start_partner_registration_failure(edge_env, monkeypatch):
    _start_env(monkeypatch, edge_env, partner_response=_Response(500))
    with pytest.raises(ValueError, match="partner account"):
        setup_core.fleet_setup_start(_start_request(edge_env))


def test_fleet_start_reuses_existing_partner_and_cleans_expired_states(
    edge_env, monkeypatch
):
    _write_fleet(
        edge_env,
        partner_token="reuse-token",
        partner_account={"domain": "example.com"},
        access_token="",
    )
    calls = _start_env(monkeypatch, edge_env)
    setup_core._FLEET_AUTH_STATES["old"] = {"expires": 0}
    result = setup_core.fleet_setup_start(_start_request(edge_env))
    assert result.auth_url.startswith("https://auth.tesla.com/")
    assert calls == []
    assert "old" not in setup_core._FLEET_AUTH_STATES


def test_fleet_start_keeps_unexpired_pending_state(edge_env, monkeypatch):
    _start_env(monkeypatch, edge_env)
    setup_core._FLEET_AUTH_STATES["live"] = {"expires": time.monotonic() + 600}
    result = setup_core.fleet_setup_start(_start_request(edge_env))
    assert result.auth_url.startswith("https://auth.tesla.com/")
    assert "live" in setup_core._FLEET_AUTH_STATES


def test_fleet_complete_rejects_missing_callback_and_state(edge_env):
    _write_fleet(edge_env)
    with pytest.raises(ValueError, match="full callback"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(authpath=str(edge_env), callback_url="https://x")
        )
    with pytest.raises(ValueError, match="state expired"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url="https://x/access?code=abc&state=unknown",
            )
        )


def test_fleet_complete_exchange_failure(edge_env, monkeypatch):
    _write_fleet(edge_env)
    setup_core._FLEET_AUTH_STATES["state-1"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    from pypowerwall.fleetapi import fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module, "_http2_request", lambda *args, **kwargs: _Response(500)
    )
    with pytest.raises(ValueError, match="authorization exchange"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url="https://x/access?code=abc&state=state-1",
            )
        )


def test_fleet_complete_requires_both_tokens(edge_env, monkeypatch):
    _write_fleet(edge_env)
    setup_core._FLEET_AUTH_STATES["state-1"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    from pypowerwall.fleetapi import fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(200, {"access_token": "only-access"}),
    )
    with pytest.raises(ValueError, match="access and refresh"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url="https://x/access?code=abc&state=state-1",
            )
        )


def test_fleet_complete_reports_live_check_failure(edge_env, monkeypatch):
    _write_fleet(edge_env)
    setup_core._FLEET_AUTH_STATES["state-1"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    from pypowerwall.fleetapi import fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200,
            {"access_token": "access", "refresh_token": "refresh"},
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda config: [setup_core.SetupFleetSite(siteid="123", name="Home")],
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda path, siteid: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = setup_core.fleet_setup_complete(
        SetupFleetCompleteRequest(
            authpath=str(edge_env),
            callback_url="https://x/access?code=abc&state=state-1",
        )
    )
    assert result.siteid == "123"
    assert "live check failed" in result.message


def test_fleet_site_rejects_unavailable_site_and_reports_live_failure(
    edge_env, monkeypatch
):
    _write_fleet(edge_env)
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites_with_refresh",
        lambda path, config: [SimpleNamespace(siteid="123")],
    )
    with pytest.raises(ValueError, match="not available"):
        setup_core.fleet_setup_site(
            SetupFleetSiteRequest(authpath=str(edge_env), siteid="999")
        )
    monkeypatch.setattr(
        setup_core,
        "_fleet_live_check",
        lambda path, siteid: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = setup_core.fleet_setup_site(
        SetupFleetSiteRequest(authpath=str(edge_env), siteid="123")
    )
    assert result.saved is True
    assert "live check failed" in result.message


def test_fleet_key_rejects_invalid_existing_private_key(edge_env):
    (edge_env / "com.tesla.3p.private-key.pem").write_text(
        "not-a-key", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="could not be read"):
        setup_core.fleet_generate_key(SetupFleetKeyRequest(authpath=str(edge_env)))


def test_fleet_v1r_refresh_and_command(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    with pytest.raises(ValueError, match="no refresh token"):
        setup_core._fleet_v1r_refresh(edge_env, {})
    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(500),
    )
    with pytest.raises(ValueError, match="unable to refresh"):
        setup_core._fleet_v1r_refresh(
            edge_env, {"refresh_token": "rt", "CLIENT_ID": "client"}
        )
    config = {"refresh_token": "rt", "CLIENT_ID": "client", "access_token": "old"}
    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200, {"access_token": "new", "refresh_token": "new-rt"}
        ),
    )
    refreshed = setup_core._fleet_v1r_refresh(edge_env, config)
    assert refreshed["access_token"] == "new"
    assert refreshed["refresh_token"] == "new-rt"

    _write_fleet(edge_env)
    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(500),
    )
    with pytest.raises(ValueError, match="registration failed"):
        setup_core._fleet_v1r_command(edge_env, "123", {})
    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ValueError, match="no response"):
        setup_core._fleet_v1r_command(edge_env, "123", {})


def test_fleet_v1r_command_retries_401_then_succeeds(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    _write_fleet(edge_env)
    responses = [_Response(401), _Response(200, {"ok": True})]
    monkeypatch.setattr(
        fleet_module, "_http2_request", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, config: {**config, "access_token": "refreshed"},
    )
    assert setup_core._fleet_v1r_command(edge_env, "123", {}) == {"ok": True}


def test_fleet_v1r_command_handles_missing_retry_response(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    _write_fleet(edge_env)
    responses = [_Response(401), None]
    monkeypatch.setattr(
        fleet_module, "_http2_request", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, config: {**config, "access_token": "refreshed"},
    )
    with pytest.raises(ValueError, match="no response"):
        setup_core._fleet_v1r_command(edge_env, "123", {})


def test_fleet_v1r_command_rejects_double_401(edge_env, monkeypatch):
    from pypowerwall.fleetapi import fleetapi as fleet_module

    _write_fleet(edge_env)
    monkeypatch.setattr(
        fleet_module, "_http2_request", lambda *args, **kwargs: _Response(401)
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_v1r_refresh",
        lambda root, config: {**config, "access_token": "refreshed"},
    )
    with pytest.raises(ValueError, match="registration failed"):
        setup_core._fleet_v1r_command(edge_env, "123", {})


def test_cloud_auth_path_validation(edge_env):
    with pytest.raises(ValueError, match="required"):
        setup_core._cloud_auth_path("")
    with pytest.raises(ValueError, match="not a directory"):
        setup_core._cloud_auth_path(str(Path.cwd() / "README.md"))


def test_cloud_auth_start_validation_and_expired_state_cleanup(edge_env, monkeypatch):
    with pytest.raises(ValueError, match="valid Tesla email"):
        setup_core.cloud_auth_start("not-an-email", str(edge_env))
    with pytest.raises(ValueError, match="region"):
        setup_core.cloud_auth_start("owner@example.com", str(edge_env), "xx")

    from pypowerwall import tesla_auth

    monkeypatch.setattr(
        tesla_auth,
        "_build_auth_url",
        lambda region="us": ("https://auth.example", "verifier", "state-new"),
    )
    setup_core._CLOUD_AUTH_STATES["old"] = {"expires": 0}
    result = setup_core.cloud_auth_start("owner@example.com", str(edge_env))
    assert result.expires_in == 600
    assert "old" not in setup_core._CLOUD_AUTH_STATES
    assert "state-new" in setup_core._CLOUD_AUTH_STATES


def test_cloud_auth_complete_errors(edge_env, monkeypatch):
    with pytest.raises(ValueError, match="code and state"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(callback_url="https://x/access")
        )
    setup_core._CLOUD_AUTH_STATES["expired"] = {
        "verifier": "v",
        "email": "owner@example.com",
        "authpath": str(edge_env),
        "region": "us",
        "expires": 0,
    }
    with pytest.raises(ValueError, match="state expired"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=expired"
            )
        )

    setup_core._CLOUD_AUTH_STATES["state"] = {
        "verifier": "v",
        "email": "owner@example.com",
        "authpath": str(edge_env),
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
    monkeypatch.setattr(
        PyPowerwallCloud,
        "connect",
        lambda self: False,
    )
    with pytest.raises(ValueError, match="connection failed"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=state"
            )
        )


def test_cloud_auth_complete_selected_site_validation(edge_env, monkeypatch):
    setup_core._CLOUD_AUTH_STATES["state"] = {
        "verifier": "v",
        "email": "owner@example.com",
        "authpath": str(edge_env),
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
        lambda self: [{"energy_site_id": 123, "site_name": "Home"}],
    )
    with pytest.raises(ValueError, match="selected siteid"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=state",
                siteid="999",
            )
        )


def test_cloud_auth_complete_multi_site_without_selection(edge_env, monkeypatch):
    setup_core._CLOUD_AUTH_STATES["state"] = {
        "verifier": "v",
        "email": "owner@example.com",
        "authpath": str(edge_env),
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
        lambda self: [
            {"energy_site_id": 123},
            {"energy_site_id": 456},
        ],
    )
    result = setup_core.cloud_auth_complete(
        SetupCloudAuthCompleteRequest(
            callback_url="https://x/access?code=abc&state=state"
        )
    )
    assert result.needs_site is True
    assert result.siteid is None


def test_cloud_auth_use_errors(edge_env, monkeypatch):
    with pytest.raises(ValueError, match="not found"):
        setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    (edge_env / ".pypowerwall.auth").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="unable to read"):
        setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    (edge_env / ".pypowerwall.auth").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="no account"):
        setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    _write_auth(edge_env)
    with pytest.raises(ValueError, match="not in the auth file"):
        setup_core.cloud_auth_use(
            SetupCloudAuthUseRequest(authpath=str(edge_env), email="other@example.com")
        )

    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: False)
    with pytest.raises(ValueError, match="unable to connect"):
        setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))


def test_cloud_auth_use_existing_site_and_multi_site(edge_env, monkeypatch):
    _write_auth(edge_env)
    (edge_env / ".pypowerwall.site").write_text("123", encoding="utf-8")
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 123}, {"energy_site_id": 456}],
    )
    result = setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    assert result.siteid == "123"
    assert result.needs_site is False

    (edge_env / ".pypowerwall.site").unlink()
    result = setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    assert result.siteid is None
    assert result.needs_site is True


def test_cloud_auth_use_ignores_unreadable_site_file(edge_env, monkeypatch):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("123", encoding="utf-8")
    original = Path.read_text

    def read_text(self, encoding=None, errors=None):
        if self == site_file:
            raise OSError("unreadable")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud, "getsites", lambda self: [{"energy_site_id": 456}]
    )
    result = setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    assert result.siteid == "456"


def test_cloud_auth_site_validation(edge_env, monkeypatch):
    with pytest.raises(ValueError, match="not a directory"):
        setup_core.cloud_auth_site(
            "owner@example.com", str(Path.cwd() / "README.md"), "1"
        )
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: False)
    with pytest.raises(ValueError, match="unable to connect"):
        setup_core.cloud_auth_site("owner@example.com", str(edge_env), "123")
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud, "getsites", lambda self: [{"energy_site_id": 123}]
    )
    with pytest.raises(ValueError, match="not available"):
        setup_core.cloud_auth_site("owner@example.com", str(edge_env), "999")
    result = setup_core.cloud_auth_site("owner@example.com", str(edge_env), "123")
    assert result.saved is True
    assert (edge_env / ".pypowerwall.site").read_text(encoding="utf-8") == "123"


def test_import_auth_tokens_validation(edge_env, monkeypatch):
    with pytest.raises(ValueError, match="valid Tesla email"):
        setup_core.import_auth_tokens(
            SetupAuthTokensRequest(authpath=str(edge_env), refresh_token="rt")
        )
    with pytest.raises(ValueError, match="refresh token"):
        setup_core.import_auth_tokens(
            SetupAuthTokensRequest(
                email="owner@example.com", authpath=str(edge_env), refresh_token=""
            )
        )
    with pytest.raises(ValueError, match="whitespace"):
        setup_core.import_auth_tokens(
            SetupAuthTokensRequest(
                email="owner@example.com",
                authpath=str(edge_env),
                refresh_token="bad token",
            )
        )
    with pytest.raises(ValueError, match="too large"):
        setup_core.import_auth_tokens(
            SetupAuthTokensRequest(
                email="owner@example.com",
                authpath=str(edge_env),
                refresh_token="x" * 16385,
            )
        )


def test_import_auth_tokens_derives_email_from_access_token(edge_env, monkeypatch):
    import pypowerwall.tesla_auth as auth_module

    monkeypatch.setattr(
        auth_module, "_extract_email_from_token", lambda token: "owner@example.com"
    )
    result = setup_core.import_auth_tokens(
        SetupAuthTokensRequest(
            authpath=str(edge_env),
            refresh_token="rt",
            access_token="at",
        )
    )
    assert result.email == "owner@example.com"
    assert result.siteid is None


def test_import_auth_file_validation(edge_env):
    with pytest.raises(ValueError, match="empty"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(mode="cloud", authpath=str(edge_env), content=" ")
        )
    with pytest.raises(ValueError, match="too large"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(
                mode="cloud",
                authpath=str(edge_env),
                content="x" * (256 * 1024 + 1),
            )
        )
    with pytest.raises(ValueError, match="not valid auth JSON"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(
                mode="cloud", authpath=str(edge_env), content="not-json"
            )
        )
    with pytest.raises(ValueError, match="no usable entries"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(mode="cloud", authpath=str(edge_env), content="[]")
        )
    with pytest.raises(ValueError, match="no usable Tesla"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(
                mode="cloud",
                authpath=str(edge_env),
                content=json.dumps({"owner@example.com": {"sso": {}}}),
            )
        )
    with pytest.raises(ValueError, match="usable .pypowerwall.fleetapi"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(
                mode="fleetapi",
                authpath=str(edge_env),
                content=json.dumps({"CLIENT_ID": "client"}),
            )
        )


def test_import_auth_tokens_clears_site_when_account_changes(edge_env):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")

    result = setup_core.import_auth_tokens(
        SetupAuthTokensRequest(
            email="new@example.com",
            authpath=str(edge_env),
            refresh_token="new-refresh",
            access_token="new-access",
        )
    )

    assert result.email == "new@example.com"
    assert result.siteid is None
    assert not site_file.exists()
    accounts = json.loads((edge_env / ".pypowerwall.auth").read_text(encoding="utf-8"))
    assert accounts["new@example.com"]["sso"]["refresh_token"] == "new-refresh"
    assert accounts["owner@example.com"]["sso"]["refresh_token"] == "owner-refresh"


def test_import_auth_tokens_keeps_site_for_same_single_account(edge_env):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")

    result = setup_core.import_auth_tokens(
        SetupAuthTokensRequest(
            email="owner@example.com",
            authpath=str(edge_env),
            refresh_token="new-refresh",
            access_token="new-access",
        )
    )

    assert result.siteid is None
    assert site_file.read_text(encoding="utf-8") == "111"


def test_import_auth_tokens_writes_explicit_site_for_changed_account(edge_env):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")

    result = setup_core.import_auth_tokens(
        SetupAuthTokensRequest(
            email="new@example.com",
            authpath=str(edge_env),
            refresh_token="new-refresh",
            access_token="new-access",
            siteid="222",
        )
    )

    assert result.siteid == "222"
    assert site_file.read_text(encoding="utf-8") == "222"


def test_import_auth_file_keeps_site_for_known_account_in_multi_account_file(
    edge_env,
):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")
    content = json.dumps(
        {
            "owner@example.com": {
                "sso": {"access_token": "new-at", "refresh_token": "new-rt"}
            },
            "other@example.com": {
                "sso": {"access_token": "other-at", "refresh_token": "other-rt"}
            },
        }
    )

    result = setup_core.import_auth_file(
        SetupAuthFileRequest(mode="cloud", authpath=str(edge_env), content=content)
    )

    assert result.email == "owner@example.com"
    assert result.siteid is None
    assert site_file.read_text(encoding="utf-8") == "111"
    accounts = json.loads((edge_env / ".pypowerwall.auth").read_text(encoding="utf-8"))
    assert set(accounts) == {"owner@example.com", "other@example.com"}


def test_import_auth_file_clears_site_for_new_active_account(edge_env):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")
    content = json.dumps(
        {
            "new@example.com": {
                "sso": {"access_token": "new-at", "refresh_token": "new-rt"}
            },
            "other@example.com": {
                "sso": {"access_token": "other-at", "refresh_token": "other-rt"}
            },
        }
    )

    result = setup_core.import_auth_file(
        SetupAuthFileRequest(mode="cloud", authpath=str(edge_env), content=content)
    )

    assert result.email == "new@example.com"
    assert result.siteid is None
    assert not site_file.exists()
    accounts = json.loads((edge_env / ".pypowerwall.auth").read_text(encoding="utf-8"))
    assert set(accounts) == {
        "owner@example.com",
        "new@example.com",
        "other@example.com",
    }


def test_import_auth_tokens_restores_auth_and_site_when_write_fails(
    edge_env, monkeypatch
):
    _write_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    original_write = setup_core._write_private_text

    def fail_auth_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.auth":
            raise OSError("disk full")
        return original_write(path, content, newline)

    monkeypatch.setattr(setup_core, "_write_private_text", fail_auth_write)
    with pytest.raises(OSError, match="disk full"):
        setup_core.import_auth_tokens(
            SetupAuthTokensRequest(
                email="new@example.com",
                authpath=str(edge_env),
                refresh_token="new-refresh",
                access_token="new-access",
            )
        )

    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_import_auth_file_restores_auth_and_site_when_write_fails(
    edge_env, monkeypatch
):
    _write_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    content = json.dumps(
        {
            "new@example.com": {
                "sso": {"access_token": "new-at", "refresh_token": "new-rt"}
            }
        }
    )
    original_write = setup_core._write_private_text

    def fail_auth_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.auth":
            raise OSError("disk full")
        return original_write(path, content, newline)

    monkeypatch.setattr(setup_core, "_write_private_text", fail_auth_write)
    with pytest.raises(OSError, match="disk full"):
        setup_core.import_auth_file(
            SetupAuthFileRequest(mode="cloud", authpath=str(edge_env), content=content)
        )

    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_imported_account_can_select_site_with_existing_workflow(edge_env, monkeypatch):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("111", encoding="utf-8")
    setup_core.import_auth_tokens(
        SetupAuthTokensRequest(
            email="new@example.com",
            authpath=str(edge_env),
            refresh_token="new-refresh",
            access_token="new-access",
        )
    )
    assert not site_file.exists()

    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [
            {"energy_site_id": 222, "site_name": "Home"},
            {"energy_site_id": 333, "site_name": "Cabin"},
        ],
    )
    use = setup_core.cloud_auth_use(
        SetupCloudAuthUseRequest(authpath=str(edge_env), email="new@example.com")
    )
    assert use.needs_site is True
    assert use.siteid is None

    selected = setup_core.cloud_auth_site("new@example.com", str(edge_env), "333")
    assert selected.siteid == "333"
    assert site_file.read_text(encoding="utf-8") == "333"


def test_merge_auth_file_handles_malformed_existing(edge_env):
    path = edge_env / ".pypowerwall.auth"
    path.write_text("not-json", encoding="utf-8")
    merged = setup_core._merge_auth_file(path, {"owner@example.com": {"sso": {}}})
    assert "owner@example.com" in merged
    assert json.loads(path.read_text(encoding="utf-8"))["owner@example.com"]


def _cloud_state(root, state, email="owner@example.com"):
    setup_core._CLOUD_AUTH_STATES[state] = {
        "verifier": "v",
        "email": email,
        "authpath": str(root),
        "region": "us",
        "expires": time.monotonic() + 600,
    }


def _cloud_token():
    return {
        "access_token": "new-at",
        "refresh_token": "new-rt",
        "expires_in": 28800,
        "token_type": "Bearer",
    }


def _old_cloud_auth(root):
    data = {
        "owner@example.com": {
            "sso": {
                "access_token": "old-at",
                "refresh_token": "old-rt",
                "expires_at": int(time.time() + 3600),
            }
        },
        "other@example.com": {
            "sso": {
                "access_token": "other-at",
                "refresh_token": "other-rt",
                "expires_at": int(time.time() + 3600),
            }
        },
    }
    (root / ".pypowerwall.auth").write_text(json.dumps(data), encoding="utf-8")
    (root / ".pypowerwall.site").write_text("111", encoding="utf-8")
    return data


def test_cloud_auth_complete_preserves_auth_when_exchange_fails(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-exchange-fail")
    from pypowerwall import tesla_auth

    def fail_exchange(*args, **kwargs):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(tesla_auth, "_exchange_code", fail_exchange)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-exchange-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_preserves_auth_when_connect_fails(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-connect-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: False)
    with pytest.raises(ValueError, match="connection failed"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-connect-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_preserves_auth_when_sites_fail(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-sites-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(PyPowerwallCloud, "getsites", lambda self: [])
    with pytest.raises(ValueError, match="no sites"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-sites-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_preserves_auth_when_temp_save_fails(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-temp-fail")
    from pypowerwall import tesla_auth

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )

    def fail_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tesla_auth, "save_token", fail_save)
    with pytest.raises(OSError, match="disk full"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-temp-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_restores_auth_when_site_write_fails(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-site-write-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 222, "site_name": "Home"}],
    )
    original_write = setup_core._write_private_text

    def flaky_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.site":
            raise OSError("disk full")
        return original_write(path, content, newline)

    monkeypatch.setattr(setup_core, "_write_private_text", flaky_write)
    with pytest.raises(OSError, match="disk full"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-site-write-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_preserves_auth_when_auth_write_fails(
    edge_env, monkeypatch
):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-auth-write-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 222, "site_name": "Home"}],
    )
    original_write = setup_core._write_private_text

    def flaky_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.auth":
            raise OSError("disk full")
        return original_write(path, content, newline)

    monkeypatch.setattr(setup_core, "_write_private_text", flaky_write)
    with pytest.raises(OSError, match="disk full"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url="https://x/access?code=abc&state=cloud-auth-write-fail"
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_preserves_files_for_invalid_siteid(edge_env, monkeypatch):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-invalid-siteid")
    from pypowerwall import tesla_auth

    def should_not_exchange(*args, **kwargs):
        raise AssertionError("token exchange must not run")

    monkeypatch.setattr(tesla_auth, "_exchange_code", should_not_exchange)
    with pytest.raises(ValueError, match="numeric"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url=("https://x/access?code=abc&state=cloud-invalid-siteid"),
                siteid="not-a-site",
            )
        )
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_removes_stale_site_for_multiple_sites(
    edge_env, monkeypatch
):
    _old_cloud_auth(edge_env)
    _cloud_state(edge_env, "cloud-multi-site")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [
            {"energy_site_id": 222, "site_name": "Home"},
            {"energy_site_id": 333, "site_name": "Cabin"},
        ],
    )
    result = setup_core.cloud_auth_complete(
        SetupCloudAuthCompleteRequest(
            callback_url="https://x/access?code=abc&state=cloud-multi-site"
        )
    )
    assert result.needs_site is True
    assert result.siteid is None
    assert not (edge_env / ".pypowerwall.site").exists()
    accounts = json.loads((edge_env / ".pypowerwall.auth").read_text(encoding="utf-8"))
    assert accounts["owner@example.com"]["sso"]["access_token"] == "new-at"
    assert accounts["other@example.com"]["sso"]["access_token"] == "other-at"


def test_fleet_complete_preserves_config_when_sites_fail(edge_env, monkeypatch):
    _write_fleet(edge_env, access_token="old-at", refresh_token="old-rt")
    config_file = edge_env / ".pypowerwall.fleetapi"
    before = config_file.read_bytes()
    setup_core._FLEET_AUTH_STATES["fleet-sites-fail"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    import pypowerwall.fleetapi.fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200, {"access_token": "new-at", "refresh_token": "new-rt"}
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda config: (_ for _ in ()).throw(ValueError("no sites")),
    )
    with pytest.raises(ValueError, match="no sites"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url=(
                    "https://example.com/access?code=abc&state=fleet-sites-fail"
                ),
            )
        )
    assert config_file.read_bytes() == before


def test_fleet_complete_preserves_config_for_invalid_refresh(edge_env, monkeypatch):
    _write_fleet(edge_env, access_token="old-at", refresh_token="old-rt")
    config_file = edge_env / ".pypowerwall.fleetapi"
    before = config_file.read_bytes()
    setup_core._FLEET_AUTH_STATES["fleet-invalid-refresh"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    import pypowerwall.fleetapi.fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(400, {"error": "invalid_grant"}),
    )
    with pytest.raises(ValueError, match="authorization exchange failed"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url=(
                    "https://example.com/access?code=abc&state=fleet-invalid-refresh"
                ),
            )
        )
    assert config_file.read_bytes() == before


def test_fleet_complete_clears_stale_site_for_multiple_sites(edge_env, monkeypatch):
    _write_fleet(edge_env, access_token="old-at", refresh_token="old-rt", site_id=111)
    setup_core._FLEET_AUTH_STATES["fleet-multi-site"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    import pypowerwall.fleetapi.fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200, {"access_token": "new-at", "refresh_token": "new-rt"}
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda config: [
            setup_core.SetupFleetSite(siteid="222"),
            setup_core.SetupFleetSite(siteid="333"),
        ],
    )
    result = setup_core.fleet_setup_complete(
        SetupFleetCompleteRequest(
            authpath=str(edge_env),
            callback_url="https://example.com/access?code=abc&state=fleet-multi-site",
        )
    )
    assert result.needs_site is True
    saved = json.loads((edge_env / ".pypowerwall.fleetapi").read_text(encoding="utf-8"))
    assert saved["site_id"] is None


def test_fleet_complete_preserves_config_when_write_fails(edge_env, monkeypatch):
    _write_fleet(edge_env, access_token="old-at", refresh_token="old-rt")
    config_file = edge_env / ".pypowerwall.fleetapi"
    before = config_file.read_bytes()
    setup_core._FLEET_AUTH_STATES["fleet-write-fail"] = {
        "authpath": str(edge_env),
        "expires": time.monotonic() + 600,
    }
    import pypowerwall.fleetapi.fleetapi as fleet_module

    monkeypatch.setattr(
        fleet_module,
        "_http2_request",
        lambda *args, **kwargs: _Response(
            200, {"access_token": "new-at", "refresh_token": "new-rt"}
        ),
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_sites",
        lambda config: [SimpleNamespace(siteid="222")],
    )
    monkeypatch.setattr(
        setup_core,
        "_fleet_write_config",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        setup_core.fleet_setup_complete(
            SetupFleetCompleteRequest(
                authpath=str(edge_env),
                callback_url=(
                    "https://example.com/access?code=abc&state=fleet-write-fail"
                ),
            )
        )
    assert config_file.read_bytes() == before


def test_cloud_auth_use_recovers_stale_site_after_interrupted_commit(
    edge_env, monkeypatch
):
    _write_auth(edge_env)
    site_file = edge_env / ".pypowerwall.site"
    site_file.write_text("999", encoding="utf-8")
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    def connect_without_stale_site(self):
        assert self.siteid is None
        return True

    monkeypatch.setattr(PyPowerwallCloud, "connect", connect_without_stale_site)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 123}, {"energy_site_id": 456}],
    )
    result = setup_core.cloud_auth_use(SetupCloudAuthUseRequest(authpath=str(edge_env)))
    assert result.needs_site is True
    assert result.siteid is None
    assert not site_file.exists()


def test_cloud_auth_complete_restores_site_when_auth_restore_fails(
    edge_env, monkeypatch
):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    site_before = site_file.read_bytes()
    _cloud_state(edge_env, "cloud-auth-restore-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 222, "site_name": "Home"}],
    )
    original_write = setup_core._write_private_text

    def flaky_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.auth":
            raise OSError("new auth write failed")
        return original_write(path, content, newline)

    restore_calls = []
    original_restore = setup_core._write_private_bytes

    def flaky_restore(path, content):
        name = Path(path).name
        restore_calls.append(name)
        if name == ".pypowerwall.auth":
            raise OSError("auth restore failed")
        return original_restore(path, content)

    monkeypatch.setattr(setup_core, "_write_private_text", flaky_write)
    monkeypatch.setattr(setup_core, "_write_private_bytes", flaky_restore)
    with pytest.raises(OSError, match="new auth write failed"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url=("https://x/access?code=abc&state=cloud-auth-restore-fail")
            )
        )
    assert restore_calls == [".pypowerwall.auth", ".pypowerwall.site"]
    assert auth_file.read_bytes() == auth_before
    assert site_file.read_bytes() == site_before


def test_cloud_auth_complete_restores_auth_when_site_restore_fails(
    edge_env, monkeypatch
):
    _old_cloud_auth(edge_env)
    auth_file = edge_env / ".pypowerwall.auth"
    site_file = edge_env / ".pypowerwall.site"
    auth_before = auth_file.read_bytes()
    _cloud_state(edge_env, "cloud-site-restore-fail")
    from pypowerwall import tesla_auth
    from pypowerwall.cloud.pypowerwall_cloud import PyPowerwallCloud

    monkeypatch.setattr(
        tesla_auth, "_exchange_code", lambda *args, **kwargs: _cloud_token()
    )
    monkeypatch.setattr(PyPowerwallCloud, "connect", lambda self: True)
    monkeypatch.setattr(
        PyPowerwallCloud,
        "getsites",
        lambda self: [{"energy_site_id": 222, "site_name": "Home"}],
    )
    original_write = setup_core._write_private_text

    def flaky_write(path, content, newline=True):
        if Path(path).name == ".pypowerwall.auth":
            raise OSError("new auth write failed")
        return original_write(path, content, newline)

    restore_calls = []
    original_restore = setup_core._write_private_bytes

    def flaky_restore(path, content):
        name = Path(path).name
        restore_calls.append(name)
        if name == ".pypowerwall.site":
            raise OSError("site restore failed")
        return original_restore(path, content)

    monkeypatch.setattr(setup_core, "_write_private_text", flaky_write)
    monkeypatch.setattr(setup_core, "_write_private_bytes", flaky_restore)
    with pytest.raises(OSError, match="new auth write failed"):
        setup_core.cloud_auth_complete(
            SetupCloudAuthCompleteRequest(
                callback_url=("https://x/access?code=abc&state=cloud-site-restore-fail")
            )
        )
    assert restore_calls == [".pypowerwall.auth", ".pypowerwall.site"]
    assert auth_file.read_bytes() == auth_before
