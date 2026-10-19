"""Boolean and integer settings take the values a proxy pypowerwall.env holds
instead of failing startup (pydantic would raise a ValidationError)."""
import logging

import pytest

from app.config import Settings


@pytest.fixture
def make_settings(monkeypatch):
    def make(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Settings()
    return make


@pytest.mark.parametrize("name, value, field, expected", [
    ("PW_DEBUG", "", "debug", False),                       # empty: default
    ("PW_NEG_SOLAR", "", "neg_solar", True),                # empty: default (yes)
    ("PW_CACHE_EXPIRE", "", "cache_expire", 5),
    ("PW_DEBUG", "no   # yes/no - Debug logging", "debug", False),   # inline comment
    ("PW_NEG_SOLAR", "no        # Set to no", "neg_solar", False),
    ("PW_TIMEOUT", "10 # seconds", "timeout", 10),
    ("PW_FAIL_FAST", " Yes ", "fail_fast", True),           # spaces and case
    ("PW_HTTPS", "http", "https_mode", True),               # proxy value
    ("PW_HTTPS", "no", "https_mode", False),
    ("PW_FAILOVER", "off", "failover", False),
])
def test_proxy_style_values_parse(make_settings, name, value, field, expected):
    assert getattr(make_settings(**{name: value}), field) == expected


@pytest.mark.parametrize("name, value, field, default", [
    ("PW_HEALTH_CHECK", "maybe", "health_check", True),
    ("PW_POOL_MAXSIZE", "abc", "pool_maxsize", 15),
])
def test_unrecognized_value_warns_and_uses_default(make_settings, caplog, name, value, field, default):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = make_settings(**{name: value})
    assert getattr(settings, field) == default
    assert f"Invalid {name}=" in caplog.text


def test_string_settings_are_used_as_given(make_settings):
    """A password may contain " #": it must not be cut as a comment."""
    assert make_settings(PW_PASSWORD="ab # cd").pw_password == "ab # cd"


def test_mqtt_controls_keeps_its_fail_closed_parsing(make_settings):
    assert make_settings(MQTT_CONTROLS="15 # all but islanding").mqtt_controls == 0
