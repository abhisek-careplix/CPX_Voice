"""Browser runtime against a real Chromium and a local page (skipped where no browser exists)."""

import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from careplix_browser.app import Settings, create_app
from careplix_browser.runtime import Runtime, playwright_key

PAGE = b"""<!doctype html><html><head><title>Payer portal (test)</title></head><body>
<form onsubmit="document.getElementById('out').textContent='submitted:'+
  document.getElementById('member').value; return false;">
  <input id="member" name="member_id" style="position:absolute;left:100px;top:100px;width:200px">
  <input id="pw" type="password" style="position:absolute;left:100px;top:160px;width:200px">
  <button id="go" type="submit" style="position:absolute;left:100px;top:220px">
    Submit request</button>
</form>
<div id="out" style="position:absolute;left:100px;top:300px"></div>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


def _chromium() -> str | None:
    for path in (
        os.environ.get("CHROMIUM_PATH", ""),
        "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome",
    ):
        if path and os.path.exists(path):
            return path
    return None


@pytest.fixture
def site():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture
def client():
    path = _chromium()
    if not path:
        pytest.skip("no Chromium available")
    settings = Settings(browser_service_token="t0k", chromium_path=path, _env_file=None)
    with TestClient(create_app(settings, Runtime(executable_path=path))) as c:
        c.headers["Authorization"] = "Bearer t0k"
        yield c


def test_key_names_are_translated():
    assert playwright_key("Return") == "Enter"
    assert playwright_key("ctrl+a") == "Control+a"
    assert playwright_key("alt+Tab") == "Alt+Tab"
    assert playwright_key("F5") == "F5"


def test_requests_need_the_token(client):
    r = client.post("/sessions", json={"start_url": ""}, headers={"Authorization": "Bearer no"})
    assert r.status_code == 401


def test_open_inspect_type_and_submit(client, site):
    opened = client.post("/sessions", json={"start_url": site, "allowed_hosts": ["127.0.0.1"]})
    assert opened.status_code == 200, opened.text
    sid = opened.json()["id"]

    shot = client.get(f"/sessions/{sid}/screenshot").json()
    assert shot["title"] == "Payer portal (test)" and shot["width"] == 1280
    assert base64.b64decode(shot["image"])[:4] == b"\x89PNG"

    button = client.post(f"/sessions/{sid}/inspect", json={"x": 130, "y": 230}).json()["element"]
    assert button["tag"] == "button" and "Submit" in button["text"] and button["in_form"]
    pw = client.post(f"/sessions/{sid}/inspect", json={"x": 150, "y": 170}).json()["element"]
    assert pw["is_password"] is True

    typed = client.post(
        f"/sessions/{sid}/act", json={"kind": "type", "x": 150, "y": 110, "text": "W123456789"}
    ).json()
    assert typed["ok"], typed
    focused = client.post(f"/sessions/{sid}/inspect", json={}).json()["element"]
    assert focused["name"] == "member_id"

    assert client.post(f"/sessions/{sid}/act", json={"kind": "click", "x": 130, "y": 230}).json()[
        "ok"
    ]
    out = client.post(f"/sessions/{sid}/inspect", json={"x": 110, "y": 305}).json()["element"]
    assert out["text"] == "submitted:W123456789"

    assert "cookies" in client.get(f"/sessions/{sid}/storage-state").json()
    assert client.delete(f"/sessions/{sid}").json() == {"ok": True}
    assert client.get(f"/sessions/{sid}/screenshot").status_code == 404


def test_other_sites_are_refused(client, site):
    sid = client.post("/sessions", json={"start_url": site, "allowed_hosts": ["127.0.0.1"]}).json()[
        "id"
    ]
    r = client.post(
        f"/sessions/{sid}/act", json={"kind": "navigate", "url": "https://example.org/"}
    )
    assert r.json()["ok"] is False and "not one of this run's sites" in r.json()["error"]
    bad = client.post(f"/sessions/{sid}/act", json={"kind": "click", "x": 5000, "y": 10}).json()
    assert bad["ok"] is False
