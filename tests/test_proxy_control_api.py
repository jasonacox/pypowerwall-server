"""The proxy's control API works on the server: GET reads without a token and
form-encoded POSTs (`curl -d "value=20&token=..."`) with the proxy's responses,
so clients such as Powerwall-Dashboard's tools/switch-mode work unchanged."""

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-secret-token"


@pytest.fixture
def client(monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "control_secret", TOKEN)
    return TestClient(app)


@pytest.fixture
def no_cloud():
    from app.core.gateway_manager import gateway_manager

    gateway_manager._cloud_control = None
    yield
    gateway_manager._cloud_control = None


@pytest.fixture(autouse=True)
def fresh_backup_cache():
    from app.api import legacy

    legacy._forget_backup_events()
    yield
    legacy._forget_backup_events()


def form(client, path, **fields):
    return client.post(f"/control/{path}", data=fields)


# ---- control off -----------------------------------------------------------


def test_reads_and_form_writes_say_disabled_without_secret(
    monkeypatch, connected_gateway
):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "control_secret", None)
    c = TestClient(app)
    disabled = {"error": "Control Commands Disabled - Set PW_CONTROL_SECRET to enable"}
    for path in ("reserve", "mode", "grid_charging", "grid_export", "max_backup"):
        r = c.get(f"/control/{path}")
        assert (r.status_code, r.json()) == (200, disabled), path  # proxy GETs: 200
    r = form(c, "reserve", value="20", token="x")
    assert (r.status_code, r.json()) == (400, disabled)


# ---- GET reads (no token) --------------------------------------------------


def test_get_reads_current_settings_without_a_token(client, connected_gateway):
    data = connected_gateway.data
    data.reserve, data.mode, data.grid_charging, data.grid_export = (
        20.0,
        "self_consumption",
        True,
        "pv_only",
    )
    assert client.get("/control/reserve").json() == {"reserve": 20.0}
    assert client.get("/control/mode").json() == {"mode": "self_consumption"}
    assert client.get("/control/grid_charging").json() == {"grid_charging": True}
    assert client.get("/control/grid_export").json() == {"grid_export": "pv_only"}


def test_get_reads_unknown_values_like_the_proxy(client, connected_gateway):
    data = connected_gateway.data
    data.reserve, data.mode, data.grid_charging, data.grid_export = (
        None,
        None,
        None,
        None,
    )
    data.system_status = {}
    assert client.get("/control/reserve").json() == {"reserve": 0}
    assert client.get("/control/mode").json() == {"mode": "unknown"}
    assert client.get("/control/grid_charging").json() == {"grid_charging": False}
    assert client.get("/control/grid_export").json() == {"grid_export": "unknown"}


# ---- form POSTs --------------------------------------------------------------


def test_form_reserve_write(client, connected_gateway, mock_pypowerwall, no_cloud):
    mock_pypowerwall.set_reserve.return_value = {"set_backup_reserve_percent": 20}
    r = form(client, "reserve", value="20", token=TOKEN)
    assert (r.status_code, r.json()) == (200, {"set_backup_reserve_percent": 20})
    mock_pypowerwall.set_reserve.assert_called_once_with(20)


def test_form_token_can_come_from_the_authorization_header(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_reserve.return_value = {"ok": True}
    r = client.post(
        "/control/reserve",
        data={"value": "20"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200


@pytest.mark.parametrize("token", ["wrong", ""])
def test_form_bad_token_is_unauthorized(
    client, connected_gateway, mock_pypowerwall, token
):
    r = form(client, "reserve", value="20", token=token)
    assert (r.status_code, r.json()) == (
        401,
        {"unauthorized": "Control Command Token Invalid"},
    )
    mock_pypowerwall.set_reserve.assert_not_called()


def test_form_empty_value_reads_the_current_setting(client, connected_gateway):
    connected_gateway.data.reserve = 35.0
    r = form(client, "reserve", token=TOKEN)
    assert (r.status_code, r.json()) == (200, {"reserve": 35.0})


@pytest.mark.parametrize(
    "path, fields, error",
    [
        ("reserve", {"value": "abc"}, "Control Command Value Invalid"),
        ("reserve", {"value": "150"}, "'value' must be between 0 and 100"),
        ("reserve", {"value": "20", "mode": "party"}, "Invalid 'mode' companion value"),
        ("mode", {"value": "party"}, "'value' must be a valid mode"),
        ("mode", {"value": "backup", "level": "x"}, "Control Command Level Invalid"),
        ("grid_charging", {"value": "maybe"}, "Control Command Value Invalid"),
        (
            "grid_export",
            {"value": "sometimes"},
            "'value' must be a valid grid export mode",
        ),
        (
            "max_backup",
            {"value": "soon"},
            "Control Command Value Invalid - use seconds or cancel",
        ),
        ("bogus", {"value": "1"}, "Invalid Command Action"),
    ],
)
def test_form_invalid_values_return_proxy_errors(
    client, connected_gateway, mock_pypowerwall, path, fields, error
):
    r = form(client, path, token=TOKEN, **fields)
    assert r.status_code == 400
    assert r.json()["error"].startswith(error)
    mock_pypowerwall.set_reserve.assert_not_called()


def test_form_mode_with_level_sets_both(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_operation.return_value = {"ok": True}
    r = form(client, "mode", value="backup", level="30", token=TOKEN)
    assert r.status_code == 200
    mock_pypowerwall.set_operation.assert_called_once_with(30, "backup")


def test_form_grid_settings_reply_like_the_proxy(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_grid_charging.return_value = {"ok": True}
    mock_pypowerwall.set_grid_export.return_value = {"ok": True}
    r = form(client, "grid_charging", value="True", token=TOKEN)
    assert (r.status_code, r.json()) == (200, {"grid_charging": "Set Successfully"})
    mock_pypowerwall.set_grid_charging.assert_called_once_with(True)
    r = form(client, "grid_export", value="pv_only", token=TOKEN)
    assert (r.status_code, r.json()) == (200, {"grid_export": "Set Successfully"})


def test_form_failed_write_is_an_error(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_reserve.return_value = None
    r = form(client, "reserve", value="20", token=TOKEN)
    assert r.status_code == 400 and "error" in r.json()


# ---- max_backup (TEDAPI v1r) -------------------------------------------------


def test_max_backup_needs_v1r(client, connected_gateway, mock_pypowerwall):
    mock_pypowerwall.tedapi_mode = "full"
    assert client.get("/control/max_backup").json() == {
        "error": "max_backup requires v1r LAN transport"
    }
    r = form(client, "max_backup", value="3600", token=TOKEN)
    assert (r.status_code, r.json()) == (
        400,
        {"error": "max_backup requires v1r LAN transport"},
    )
    r = client.post(
        "/control/max_backup", json={"value": 3600}, headers={"Authorization": TOKEN}
    )
    assert r.status_code == 503
    mock_pypowerwall.schedule_max_backup.assert_not_called()


def test_max_backup_on_v1r(client, connected_gateway, mock_pypowerwall):
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.return_value = {
        "manual_backup": {"active": True}
    }
    mock_pypowerwall.schedule_max_backup.return_value = True
    mock_pypowerwall.cancel_max_backup.return_value = True
    assert client.get("/control/max_backup").json() == {
        "manual_backup": {"active": True}
    }
    r = form(client, "max_backup", value="3600", token=TOKEN)
    assert (r.status_code, r.json()) == (
        200,
        {"max_backup": "Scheduled for 3600 seconds"},
    )
    mock_pypowerwall.schedule_max_backup.assert_called_once_with(3600)
    r = form(client, "max_backup", value="cancel", token=TOKEN)
    assert (r.status_code, r.json()) == (200, {"max_backup": "Cancelled"})
    r = client.post(
        "/control/max_backup",
        json={"value": "cancel"},
        headers={"Authorization": TOKEN},
    )
    assert (r.status_code, r.json()) == (200, {"max_backup": "Cancelled"})


def test_max_backup_get_cancels_an_expired_event_only_with_a_token(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.side_effect = lambda: {
        "manual_backup": {"active": False}
    }
    assert client.get("/control/max_backup").json() == {
        "manual_backup": {"active": False}
    }
    mock_pypowerwall.cancel_max_backup.assert_not_called()
    assert client.get(f"/control/max_backup?token={TOKEN}").json() == {
        "manual_backup": None
    }
    mock_pypowerwall.cancel_max_backup.assert_called_once()
    # The token GET decided on fresh data, not the cached list
    assert mock_pypowerwall.get_backup_events.call_count == 2


# ---- JSON API unchanged --------------------------------------------------------


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_json_body_must_be_an_object(client, connected_gateway, body):
    r = client.post(
        "/control/reserve",
        content=body,
        headers={"Authorization": TOKEN, "Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_max_backup_list_is_cached_and_writes_drop_the_cache(
    client, connected_gateway, mock_pypowerwall
):
    """The unauthenticated GET must not turn every request into a gateway call."""
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.return_value = {"manual_backup": None}
    mock_pypowerwall.schedule_max_backup.return_value = True
    client.get("/control/max_backup")
    client.get("/control/max_backup")
    assert mock_pypowerwall.get_backup_events.call_count == 1
    form(client, "max_backup", value="600", token=TOKEN)
    client.get("/control/max_backup")
    assert mock_pypowerwall.get_backup_events.call_count == 2


def test_max_backup_failed_cancel_keeps_the_event(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.return_value = {
        "manual_backup": {"active": False}
    }
    mock_pypowerwall.cancel_max_backup.return_value = None  # timeout/error
    r = client.get(f"/control/max_backup?token={TOKEN}")
    assert (r.status_code, r.json()) == (200, {"error": "Failed to cancel max backup"})


def test_every_max_backup_gateway_call_holds_the_lock(
    client, connected_gateway, mock_pypowerwall, monkeypatch
):
    """List, cleanup cancel and schedule run under one lock, so the GET's
    read/check/cancel can't interleave with a POST that schedules."""
    from app.api import legacy
    from app.core.gateway_manager import gateway_manager

    mock_pypowerwall.tedapi_mode = "v1r"
    seen = []

    async def fake_local_control(gateway_id, method, *args, timeout=10.0, **kwargs):
        seen.append((method, legacy._max_backup_lock.locked()))
        return (
            {"manual_backup": {"active": False}}
            if method == "get_backup_events"
            else True
        )

    monkeypatch.setattr(gateway_manager, "local_control", fake_local_control)
    client.get(f"/control/max_backup?token={TOKEN}")
    form(client, "max_backup", value="600", token=TOKEN)
    client.post(
        "/control/max_backup",
        json={"value": "cancel"},
        headers={"Authorization": TOKEN},
    )
    form(client, "max_backup", token=TOKEN)  # empty value: a read
    assert [m for m, _ in seen] == [
        "get_backup_events",
        "cancel_max_backup",
        "schedule_max_backup",
        "cancel_max_backup",
        "get_backup_events",
    ]
    assert all(locked for _, locked in seen)


def test_max_backup_writes_share_the_gateway_write_lock():
    from app.core.gateway_manager import _WRITE_METHODS

    assert {"schedule_max_backup", "cancel_max_backup"} <= _WRITE_METHODS


@pytest.mark.parametrize(
    "path, value, library_result, error",
    [
        ("grid_charging", "true", {"ERROR": "site offline"}, "site offline"),
        ("grid_charging", "true", False, "Control command failed for grid_charging"),
        ("grid_export", "never", {"error": "nope"}, "nope"),
        ("reserve", "20", {"ERROR": "Tesla said no"}, "Tesla said no"),
    ],
)
def test_form_reports_failures_the_dispatch_returns(
    client,
    connected_gateway,
    mock_pypowerwall,
    no_cloud,
    path,
    value,
    library_result,
    error,
):
    """A failure result must never come back as "Set Successfully"."""
    getattr(mock_pypowerwall, f"set_{path}").return_value = library_result
    r = form(client, path, value=value, token=TOKEN)
    assert (r.status_code, r.json()) == (400, {"error": error})


def test_openapi_declares_json_and_form_bodies():
    from app.main import app

    content = app.openapi()["paths"]["/control/{path}"]["post"]["requestBody"][
        "content"
    ]
    assert {"application/json", "application/x-www-form-urlencoded"} <= set(content)


def test_max_backup_failed_read_is_cached_too(
    client, connected_gateway, mock_pypowerwall
):
    """A failure storm on the unauthenticated GET makes one gateway call."""
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.return_value = None
    for _ in range(5):
        assert client.get("/control/max_backup").json() == {
            "error": "Failed to get backup events"
        }
    assert mock_pypowerwall.get_backup_events.call_count == 1


def test_non_ascii_tokens_are_rejected_not_500(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.get_backup_events.return_value = {
        "manual_backup": {"active": False}
    }
    assert form(client, "reserve", value="20", token="é").status_code == 401
    r = client.get("/control/max_backup?token=%C3%A9")
    assert r.status_code == 200
    mock_pypowerwall.cancel_max_backup.assert_not_called()
    # A non-ASCII Authorization header (bytes on the wire) on the JSON API too
    r = client.post(
        "/control/reserve",
        json={"value": 20},
        headers={"Authorization": "Bearer é".encode("utf-8")},
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    "path, fields",
    [
        ("reserve", {"value": "²"}),
        ("mode", {"value": "backup", "level": "²"}),
        ("max_backup", {"value": "²"}),
    ],
)
def test_unicode_digits_are_invalid_not_500(client, connected_gateway, path, fields):
    r = form(client, path, token=TOKEN, **fields)
    assert r.status_code == 400 and "error" in r.json()


def test_max_backup_under_60_seconds_is_refused(
    client, connected_gateway, mock_pypowerwall
):
    """pypowerwall clamps shorter durations to 60 s; don't report 30 as scheduled."""
    mock_pypowerwall.tedapi_mode = "v1r"
    r = form(client, "max_backup", value="30", token=TOKEN)
    assert r.status_code == 400 and "60 to 4294967295" in r.json()["error"]
    r = client.post(
        "/control/max_backup", json={"value": 30}, headers={"Authorization": TOKEN}
    )
    assert r.status_code == 400
    mock_pypowerwall.schedule_max_backup.assert_not_called()


@pytest.mark.asyncio
async def test_max_backup_write_keeps_the_write_lock_until_the_call_finishes(
    mock_gateway_manager, connected_gateway, mock_pypowerwall
):
    """After our timeout the library call can still be running (cancel plus
    schedule): the next write must wait for it."""
    import asyncio
    import threading
    from app.core.gateway_manager import gateway_manager

    release = threading.Event()
    mock_pypowerwall.schedule_max_backup.side_effect = (
        lambda seconds: release.wait(5) or True
    )
    result = await gateway_manager.local_control(
        "test-gateway", "schedule_max_backup", 600, timeout=0.05
    )
    assert result is None  # timed out
    assert gateway_manager._write_lock.locked()  # still held by the running call
    release.set()
    for _ in range(100):
        if not gateway_manager._write_lock.locked():
            break
        await asyncio.sleep(0.01)
    assert not gateway_manager._write_lock.locked()


def test_max_backup_timeout_covers_two_round_trips(monkeypatch):
    from app.api import legacy
    from app.config import settings

    monkeypatch.setattr(settings, "timeout", 10)
    assert legacy._max_backup_timeout() == 45


def test_form_body_without_content_type_is_read_as_a_form(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    """The proxy reads any POST body as a form (e.g. requests.post(data=str))."""
    mock_pypowerwall.set_reserve.return_value = {"ok": True}
    r = client.post(
        "/control/reserve",
        content=f"value=20&token={TOKEN}",
        headers={"Content-Type": ""},
    )
    assert r.status_code == 200
    mock_pypowerwall.set_reserve.assert_called_once_with(20)


def test_json_without_content_type_still_works_and_text_plain_is_422(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_reserve.return_value = {"ok": True}
    r = client.post(
        "/control/reserve",
        content='{"value": 20}',
        headers={"Authorization": TOKEN, "Content-Type": ""},
    )
    assert r.status_code == 200
    r = client.post(
        "/control/reserve",
        content='{"value": 20}',
        headers={"Authorization": TOKEN, "Content-Type": "text/plain"},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # FastAPI's validation shape, as before


def test_oversized_form_body_is_refused_before_reading(client, connected_gateway):
    r = client.post(
        "/control/reserve",
        content="value=" + "1" * 5000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert (r.status_code, r.json()) == (
        400,
        {"error": "Control Command Error: Invalid Request"},
    )


@pytest.mark.asyncio
async def test_cleanup_waits_for_a_timed_out_schedule_before_reading(
    monkeypatch, mock_gateway_manager, connected_gateway, mock_pypowerwall
):
    """A schedule that timed out but is still running must finish before the
    GET cleanup reads the events, so the cleanup sees the new event and
    doesn't cancel it."""
    import asyncio
    import threading
    from app.api import legacy
    from app.config import settings
    from app.core.gateway_manager import gateway_manager

    monkeypatch.setattr(settings, "control_secret", TOKEN)
    mock_pypowerwall.tedapi_mode = "v1r"
    state = {"manual_backup": {"active": False}}  # an expired event
    release = threading.Event()

    def slow_schedule(seconds):
        release.wait(5)
        state["manual_backup"] = {"active": True}  # the new event lands
        return True

    mock_pypowerwall.schedule_max_backup.side_effect = slow_schedule
    mock_pypowerwall.get_backup_events.side_effect = lambda: dict(state)
    mock_pypowerwall.cancel_max_backup.return_value = True

    result = await gateway_manager.local_control(
        "test-gateway", "schedule_max_backup", 600, timeout=0.05
    )
    assert result is None  # timed out; the worker is still running
    cleanup = asyncio.create_task(
        legacy.control_read_max_backup(token=TOKEN, authorization=None)
    )
    await asyncio.sleep(0.1)
    assert not cleanup.done()  # waiting for the running schedule
    mock_pypowerwall.get_backup_events.assert_not_called()
    release.set()
    response = await asyncio.wait_for(cleanup, 5)
    assert response.status_code == 200
    mock_pypowerwall.cancel_max_backup.assert_not_called()  # saw the new event


@pytest.mark.parametrize("seconds", [2**32, 10**12])
def test_max_backup_duration_over_uint32_never_dispatches(
    client, connected_gateway, mock_pypowerwall, seconds
):
    """Scheduling cancels the current event first: an out-of-range duration
    must be refused before any write reaches the library."""
    mock_pypowerwall.tedapi_mode = "v1r"
    r = form(client, "max_backup", value=str(seconds), token=TOKEN)
    assert r.status_code == 400 and "error" in r.json()
    r = client.post(
        "/control/max_backup", json={"value": seconds}, headers={"Authorization": TOKEN}
    )
    assert r.status_code == 400
    mock_pypowerwall.schedule_max_backup.assert_not_called()
    mock_pypowerwall.cancel_max_backup.assert_not_called()


def test_max_backup_accepts_the_uint32_maximum(
    client, connected_gateway, mock_pypowerwall
):
    mock_pypowerwall.tedapi_mode = "v1r"
    mock_pypowerwall.schedule_max_backup.return_value = True
    r = form(client, "max_backup", value=str(2**32 - 1), token=TOKEN)
    assert r.status_code == 200
    mock_pypowerwall.schedule_max_backup.assert_called_once_with(2**32 - 1)


@pytest.mark.parametrize(
    "media_type", ["Application/JSON", "APPLICATION/JSON; charset=utf-8"]
)
def test_json_media_type_is_case_insensitive(
    client, connected_gateway, mock_pypowerwall, no_cloud, media_type
):
    mock_pypowerwall.set_reserve.return_value = {"ok": True}
    r = client.post(
        "/control/reserve",
        content='{"value": 20}',
        headers={"Authorization": TOKEN, "Content-Type": media_type},
    )
    assert r.status_code == 200


def test_form_media_type_is_case_insensitive(
    client, connected_gateway, mock_pypowerwall, no_cloud
):
    mock_pypowerwall.set_reserve.return_value = {"ok": True}
    r = client.post(
        "/control/reserve",
        content=f"value=20&token={TOKEN}",
        headers={"Content-Type": "Application/X-Www-Form-Urlencoded"},
    )
    assert r.status_code == 200
