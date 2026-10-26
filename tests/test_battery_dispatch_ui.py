"""Console dispatch markup and browser controller behavior without hardware."""

from pathlib import Path
import shutil
import subprocess

from bs4 import BeautifulSoup
import pytest


def test_console_serves_accessible_dispatch_form_and_script(client):
    html = BeautifulSoup(client.get("/console").text, "html.parser")
    panel = html.find(id="control-dispatch-section")
    assert panel["style"] == "display:none"
    assert panel["aria-labelledby"] == "control-dispatch-title"
    assert panel.find(id="control-dispatch-power")["min"] == "0"
    duration = panel.find(id="control-dispatch-duration")
    assert (duration["min"], duration["max"], duration["step"]) == ("1", "86400", "1")
    assert panel.find(id="control-dispatch-start")["type"] == "submit"
    assert panel.find(id="control-dispatch-resume")["type"] == "button"
    assert panel.find(id="control-dispatch-message")["aria-live"] == "polite"
    assert "drain below your Backup Reserve" in panel.get_text()
    control_script = next(
        script.text for script in html.find_all("script") if "initControl()" in script.text
    )
    assert "confirmDialog," in control_script
    script = next(
        s["src"]
        for s in html.find_all("script", src=True)
        if "/static/js/battery-dispatch.js" in s["src"]
    )
    response = client.get(script)
    assert response.status_code == 200
    assert "window.PWBatteryDispatch" in response.text


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is needed for UI behavior tests"
)
def test_dispatch_ui_behavior():
    suite = Path(__file__).with_name("battery_dispatch_ui.cjs")
    result = subprocess.run(
        [shutil.which("node"), "--test", str(suite)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
