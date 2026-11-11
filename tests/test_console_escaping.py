"""Console renders server and config strings as text, never as markup."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_console_escapes_gateway_list_values(client):
    html = client.get("/console").text
    assert "function escapeHtml(value)" in html
    for value in ("gw.name || id", "hostInfo", "mode", "firmwareVersion", "status.error"):
        assert f"${{escapeHtml({value})}}" in html, value
    # No raw interpolation of the error string anywhere in the page
    assert not re.search(r"\$\{\s*status\.error\s*\}", html)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is needed for UI behavior tests"
)
def test_console_renderers_escape_markup():
    suite = Path(__file__).with_name("console_escape_html.cjs")
    result = subprocess.run(
        [shutil.which("node"), "--test", str(suite)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
