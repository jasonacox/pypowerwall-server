"""Pydantic validator edge cases for setup request models."""

import pytest

from app.models.setup import SetupGatewayInput


def _gateway(**overrides):
    values = {
        "id": "home",
        "mode": "tedapi",
        "host": "192.168.91.1",
        "gw_pwd": "secret",
    }
    values.update(overrides)
    return SetupGatewayInput(**values)


def test_gateway_id_is_trimmed_and_validated():
    assert _gateway(id="  home-1_2  ").id == "home-1_2"
    with pytest.raises(ValueError, match="required"):
        _gateway(id="   ")
    with pytest.raises(ValueError, match="64"):
        _gateway(id="x" * 65)
    with pytest.raises(ValueError, match="only letters"):
        _gateway(id="bad id")


def test_gateway_type_is_normalized_and_validated():
    assert _gateway(type=" Inverter ").type == "inverter"
    assert _gateway(type="POWERWALL").type == "powerwall"
    with pytest.raises(ValueError, match="powerwall.*inverter"):
        _gateway(type="meter")


def test_gateway_email_normalization_and_validation():
    assert _gateway(email=None).email is None
    assert _gateway(email="   ").email is None
    assert _gateway(email=" owner@example.com ").email == "owner@example.com"
    with pytest.raises(ValueError, match="valid Tesla email"):
        _gateway(email="not-an-email")
    with pytest.raises(ValueError, match="valid Tesla email"):
        _gateway(email="owner@example.com with-space")
    with pytest.raises(ValueError, match="valid Tesla email"):
        _gateway(email="owner@example")


def test_gateway_siteid_normalization():
    assert _gateway(siteid=None).siteid is None
    assert _gateway(siteid=12345).siteid == "12345"
    assert _gateway(siteid=" 12345 ").siteid == "12345"
    assert _gateway(siteid="   ").siteid is None
