"""Static and syntax checks for the setup wizard UI."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_setup_page_uses_shared_assets_and_placeholders():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    assert "{PROXY_BASE_SCRIPT}" in html
    assert "{PROXY_BASE}" in html
    assert "{SERVER_VERSION}" in html
    assert "/static/css/setup.css" in html
    assert "/static/js/setup.js" in html
    assert "setup-token" in html
    assert "/api/setup" not in html


def test_setup_javascript_uses_local_token_storage_and_token_header():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "localStorage" in js
    assert "sessionStorage" in js
    assert "SETUP_TOKEN_KEY" in js
    assert "SETUP_REMEMBER_KEY" in js
    assert "pw_setup_token" in js
    assert "pw_setup_remember" in js
    assert "initSetupTokenStorage" in js
    assert "clearStoredSetupToken" in js
    assert "X-PW-Setup-Token" in js
    assert '"/control/' not in js
    assert 'fetch("/control' not in js
    assert "/api/setup" in js


def test_setup_token_remember_control_matches_console_wording():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "css" / "setup.css").read_text(encoding="utf-8")
    assert 'id="setup-remember"' in html
    assert "Remember my token on this device" in html
    assert ".control-remember" in css


def test_setup_secret_fields_use_console_show_hide_controls():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "css" / "setup.css").read_text(encoding="utf-8")
    assert 'data-secret-label="token"' in html
    assert "enhanceSecretInputs" in js
    assert "querySelectorAll('input[type=\"password\"]')" in js
    assert 'button.className = "control-btn control-icon-btn"' in js
    assert "SECRET_EYE_OPEN" in js
    assert "SECRET_EYE_CLOSED" in js
    assert ".control-token-row" in css
    assert ".control-icon-btn" in css


def test_setup_uses_dropdowns_for_documented_enumerations():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    for value in (
        "clear",
        "black",
        "white",
        "grafana",
        "grafana-dark",
        "solar",
        "dakboard",
    ):
        assert value in js
    assert "mqtt_qos" in js
    assert "mqtt_controls" in js
    assert "MQTT_CONTROL_CHOICES = [" in js
    assert "length: 64" not in js
    assert 'value: "47"' in js
    assert 'value: "63"' in js
    assert "Monitoring only" in js
    assert "fleetapi_http" in js
    assert 'value: "1.1"' in js
    assert 'auth_mode: [{ value: "cookie", label: "cookie" }]' in js
    assert "settingDefaultHint" in js
    assert "Server default:" in js
    assert 'typeof current === "boolean" ? current : Boolean(field.default)' in js


def test_setup_field_hints_use_info_dialog():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "css" / "setup.css").read_text(encoding="utf-8")
    assert 'id="setup-info-dialog"' in html
    assert "data-info-field" in html
    assert "enhanceFieldHints" in js
    assert "openFieldInfo" in js
    assert "INFO_ICON" in js
    assert "button.dataset.infoTitle = labelText" in js
    assert "hint.hidden = true" in js
    assert ".control-label-row" in css
    assert ".confirm-dialog" in css


def test_setup_settings_are_collapsed_and_ordered():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "details.open = false" in js
    assert "const SETTING_GROUP_ORDER" in js
    assert "const SETTING_ORDER" in js
    assert '"Local and WiFi fallback"' in js
    assert '"Cloud and failover"' in js
    assert 'Port <span class="optional">(optional)</span>' in html
    assert 'Site ID <span class="optional">(optional)</span>' in html
    assert 'WiFi host <span class="optional">(optional)</span>' in html
    assert "settingIsOptional" in js
    assert 'optional.textContent = " (optional)"' in js


def test_setup_auth_file_import_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "cloud-auth-file" in html
    assert "fleet-auth-file" in html
    assert "Import Cloud auth file" in html
    assert "Import FleetAPI config" in html
    assert "uploadAuthFile" in js
    assert 'api("/auth/file"' in js


def test_setup_network_scan_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'id="scan-cidr"' in html
    assert 'id="scan-gateways"' in html
    assert 'id="scan-output"' in html
    assert 'id="scan-results"' in html
    assert 'class="scan-field"' in html
    assert '<div class="scan-section collapsed" id="scan-output">' in html
    assert 'id="scan-output-toggle"' in html
    assert 'aria-expanded="false"' in html
    assert "Network scan" in html
    assert "scan-output-toggle" in js
    assert "scanNetwork" in js
    assert 'api("/scan"' in js


def test_setup_timezone_input_uses_server_datalist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'list="timezone-options"' in html
    assert '<datalist id="timezone-options">' in html
    assert "state.schema.timezones" in js
    assert "populateTimezones" in js


def test_setup_v1r_registration_is_in_gui():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "data-v1r-registration" in html
    assert "Tesla key registration" in html
    assert "v1r-register" in html
    assert "v1r/register/status" in js
    assert "Open Tesla login" in html
    assert "v1r-login-result" in html
    assert "v1r-auth-status" in html
    assert "v1r-step-title" in html
    assert "physical proof on the powerwall" in html.lower()
    assert "v1r-auth-file" in html
    assert "use an existing tesla login" in html.lower()
    assert "Choose .pypowerwall.auth" in html
    assert "No file selected" in html
    assert "Upload auth file" in html
    assert "404 error; this is expected" in html
    assert "uploadV1rAuthFile" in js
    assert "v1r/auth-file" in js


def test_setup_gui_does_not_require_cli_setup_commands():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    gui = html + "\n" + js
    for command in (
        "python -m pypowerwall setup",
        "python3 -m pypowerwall setup",
        "pypowerwall setup",
        "pypowerwall register",
        "pypowerwall-server --setup",
        "setup -fleetapi",
    ):
        assert command not in gui
    assert "docker logs pypowerwall-server" in html


def test_setup_fleetapi_setup_is_in_gui():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "data-fleet-registration" in html
    assert "Fleet API setup" in html
    assert "fleet-client-id" in html
    assert "fleet-client-secret" in html
    assert "fleet-key" in html
    assert "Copy public key" in html
    assert "fleet-start" in html
    assert "fleet-complete" in html
    assert "fleetapi/key" in js
    assert "fleetapi/start" in js
    assert "fleetapi/complete" in js
    assert "fleetapi/site" in js


def test_setup_contains_repo_warning_text_verbatim():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "WARNING: USE AT YOUR OWN RISK. IT COULD VOID YOUR WARRANTY." in js
    assert "WARNING: USE AT YOUR OWN RISK" in js
    assert (
        "WARNING — Grid island control physically operates your home's grid contactor. Use with extreme care."
        in js
    )
    assert "leave you <strong>without backup power</strong>" in js
    assert "accept full responsibility for the results" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_setup_javascript_syntax():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["node", "--check", str(root / "app" / "static" / "js" / "setup.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is needed for UI behavior tests"
)
def test_setup_mode_ui_behavior():
    suite = Path(__file__).with_name("setup_mode_ui.cjs")
    result = subprocess.run(
        [shutil.which("node"), "--test", str(suite)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_setup_v1r_fleetapi_registration_button_exists():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "v1r-register-fleet" in html
    assert "Register with FleetAPI" in html
    assert 'registerV1rKey(card, "fleetapi")' in js
    assert "method," in js


def test_setup_hybrid_fleetapi_mode_and_auth_panels_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert '<option value="hybrid_fleetapi">Hybrid FleetAPI</option>' in html
    assert "hybrid_fleetapi:" in js
    assert 'const v1rModes = ["tedapi_v1r", "hybrid", "hybrid_fleetapi"]' in js
    assert 'const cloudControlModes = ["cloud", "hybrid"]' in js
    assert 'const fleetControlModes = ["fleetapi", "hybrid_fleetapi"]' in js
    assert 'const authModes = ["cloud", "fleetapi", "hybrid", "hybrid_fleetapi"]' in js
    assert 'gatewayMode === "hybrid_fleetapi"' in js


def test_setup_delete_config_and_rename_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'id="delete-config"' in html
    assert "Delete configuration file" in html
    assert 'api("/delete-config"' in js
    assert "async function deleteConfig" in js
    assert "expected_revision: state.status.config_file_revision" in js
    assert "card.dataset.originalId = config.id" in js
    assert "data.original_id = card.dataset.originalId" in js


def test_setup_malformed_config_confirmation_and_warnings_exist():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "could not be parsed at startup" in js
    assert "Saving will replace it and write a backup" in js
    assert "result.warnings" in js


def test_setup_env_import_activation_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "activate-env" in html
    assert "Environment configuration migration" in html
    assert "Write environment configuration to YAML" in html
    assert "env-import-help" in html
    assert "Nothing has been written yet" in js
    assert "environment configuration to ${result.target_path}" in js
    assert "window.confirm(" in js
    assert "control secrets" in js
    assert "async function activateEnv" in js
    assert 'api("/import-env", {' in js


def test_setup_control_secret_generator_exists():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'input.id === "setting-control_secret"' in js
    assert "Generate a random control token" in js
    assert "crypto.getRandomValues" in js


def test_setup_headless_token_paste_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "cloud-refresh-token" in html
    assert "cloud-access-token" in html
    assert "Save pasted tokens" in html
    assert "importCloudTokens" in js
    assert 'api("/auth/tokens"' in js


def test_setup_documented_placeholder_examples_match_repo_docs():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    docs = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("README.md", "API.md", "gateways.yaml.example")
    )
    for example in (
        "home",
        "Home System",
        "192.168.91.1",
        "last 5 characters of the gateway password",
        "/keys/tedapi_rsa_private.pem",
        "user@example.com",
        "/auth",
        "example.com",
    ):
        assert f'placeholder="{example}"' in html, example
        assert example in docs, example


def test_setup_ui_specific_placeholders_are_explicit():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    for text in (
        "Private CIDR (optional), e.g. 192.168.1.0/24",
        "Search or select a time zone",
        "123456789",
        "Leave blank",
        "Paste the full tesla://auth/callback URL",
        "Paste the full callback URL",
        "Tesla developer Client ID",
        "Tesla developer Client Secret",
        "https://example.com/access",
        "Refresh Token (RT)",
        "Access Token (AT, optional)",
    ):
        assert f'placeholder="{text}"' in html, text
    assert 'input.placeholder = "Leave blank";' in js


def test_setup_stored_settings_secrets_can_be_cleared():
    root = Path(__file__).resolve().parent.parent
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert "Clear the stored secret" in js
    assert "secretCleared" in js
    assert "options[name] = null;" in js


def test_setup_cloud_region_and_existing_auth_controls_exist():
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "static" / "setup.html").read_text(encoding="utf-8")
    js = (root / "app" / "static" / "js" / "setup.js").read_text(encoding="utf-8")
    assert 'class="cloud-region"' in html
    assert "Use existing Cloud auth" in html
    assert 'api("/auth/cloud/use"' in js
    assert "region: region" in js
    assert "Overwrite it with a new Tesla login" in js
