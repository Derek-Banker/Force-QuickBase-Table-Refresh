"""Exercise the headless browser against a local fake Quickbase page."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from qb_refresh import RefreshError, RefreshSettings, TableTarget, refresh_table


LOGIN = b"""<!doctype html><form action='/login' method='post'>
<input name='loginid' type='text'><input name='password' type='password'>
<button type='submit' name='SignIn' id='signin'>Sign in</button></form>"""

TABLE = b"""<!doctype html><button id='refresh' disabled>Refresh Data</button>
<script>
fetch('/qb/sync/refreshTasks/100001?getInitialStatus=true').then(() => {
  document.querySelector('#refresh').disabled = false;
});
document.querySelector('#refresh').addEventListener('click', () => {
  fetch('/qb/sync/refreshTasks?notification=ButtonClick&replicationID=100001&token=local-placeholder', {method:'POST'});
});
</script>"""


TABLE_WITHOUT_INITIAL_STATUS = b"""<!doctype html><button id='refresh'>Refresh Data</button>
<script>
document.querySelector('#refresh').addEventListener('click', () => {
  const query = new URLSearchParams();
  query.set('replication' + 'ID', String(100001));
  query.set('token', 'local-placeholder');
  fetch('/qb/sync/refreshTasks?' + query, {method:'POST'});
});
</script>"""


class FakeQuickbase(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.mode = mode
        self.refresh_posts = []
        self.logins = []
        self.polls_after_start = 0

    def payload(self):
        old = {"id": 100, "replicationId": "100001", "status": 3}
        result = {"currentRefresh": None, "lastRefresh": old, "lastGoodRefresh": old, "lastError": None}
        if self.refresh_posts:
            if self.mode == "captcha":
                result["lastError"] = "CAPTCHA evaluation failed token=local-placeholder"
            elif self.mode == "delayed" and self.polls_after_start == 0:
                pass
            elif self.polls_after_start < 2 and self.mode != "late-fast":
                result["currentRefresh"] = {"id": 101, "replicationId": "100001", "status": 1}
            else:
                result["lastRefresh"] = {"id": 101, "replicationId": "100001", "status": 3}
                result["lastGoodRefresh"] = result["lastRefresh"]
        return result


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type="application/json"):
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/qb/sync/refreshTasks/"):
            if self.server.refresh_posts:
                self.server.polls_after_start += 1
            self.send(self.server.payload())
        elif "fake-session=valid" in self.headers.get("Cookie", ""):
            if self.server.mode == "iframe" and urlsplit(self.path).path == "/table":
                table = b"<!doctype html><iframe src='/legacy' title='Connected table'></iframe>"
            else:
                table = TABLE_WITHOUT_INITIAL_STATUS if self.server.mode.startswith("late") else TABLE
            self.send(table, "text/html")
        else:
            self.send(LOGIN, "text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if urlsplit(self.path).path == "/login":
            self.server.logins.append(parse_qs(body.decode()))
            self.send_response(303)
            self.send_header("Location", "/table")
            self.send_header("Set-Cookie", "fake-session=valid; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.server.refresh_posts.append(parse_qs(urlsplit(self.path).query))
            self.send(self.server.payload())


@pytest.fixture
def browser_available():
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright
    async def executable():
        async with async_playwright() as playwright:
            return playwright.chromium.executable_path
    if not Path(asyncio.run(executable())).exists():
        pytest.skip("Install the Playwright Chromium browser to run the local integration test.")


@pytest.fixture
def fake_server(request):
    server = FakeQuickbase(request.param)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings_for(server, tmp_path):
    realm = f"http://127.0.0.1:{server.server_port}"
    # The file loader deliberately rejects local HTTP. The test injects settings
    # directly so that no browser request can reach the real Quickbase realm.
    return RefreshSettings(
        realm_url=realm,
        tables={"users": TableTarget(realm + "/table")},
        state_file=tmp_path / ".state" / "browser.json",
        navigation_timeout_seconds=5,
        start_timeout_seconds=5,
        refresh_timeout_seconds=5,
        poll_interval_seconds=0.05,
    )


@pytest.mark.parametrize("fake_server", ["success"], indirect=True)
def test_headless_login_click_and_matching_completion(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    settings = settings_for(fake_server, tmp_path)
    result = asyncio.run(refresh_table(settings, "users"))

    assert result.status == "completed"
    assert result.replication_id == "100001"
    assert result.refresh_id == "101"
    assert fake_server.logins == [{"loginid": ["local@example.test"], "password": ["local-test-password"]}]
    assert len(fake_server.refresh_posts) == 1
    assert fake_server.refresh_posts[0]["replicationID"] == ["100001"]
    assert fake_server.polls_after_start >= 2
    assert settings.state_file.exists()
    assert "local-test-password" not in settings.state_file.read_text(encoding="utf-8")

    # The saved session supports later API calls without another password login.
    monkeypatch.delenv("QB_USERNAME")
    monkeypatch.delenv("QB_PASSWORD")
    fake_server.refresh_posts.clear()
    fake_server.polls_after_start = 0
    second = asyncio.run(refresh_table(settings, "users"))
    assert second.refresh_id == "101"
    assert len(fake_server.logins) == 1
    assert len(fake_server.refresh_posts) == 1


@pytest.mark.parametrize("fake_server", ["captcha"], indirect=True)
def test_headless_captcha_rejection_stops_without_retry(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    with pytest.raises(RefreshError) as raised:
        asyncio.run(refresh_table(settings_for(fake_server, tmp_path), "users"))
    assert raised.value.code == "CAPTCHA_FAILED"
    assert "local-placeholder" not in raised.value.message
    assert len(fake_server.refresh_posts) == 1


@pytest.mark.parametrize("fake_server", ["late"], indirect=True)
def test_headless_discovers_identifier_from_the_button_post(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    result = asyncio.run(refresh_table(settings_for(fake_server, tmp_path), "users"))
    assert result.replication_id == "100001"
    assert result.refresh_id == "101"
    assert len(fake_server.refresh_posts) == 1


@pytest.mark.parametrize("fake_server", ["late-fast"], indirect=True)
def test_headless_without_a_baseline_cannot_accept_last_success(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    with pytest.raises(RefreshError) as raised:
        asyncio.run(refresh_table(settings_for(fake_server, tmp_path), "users"))
    assert raised.value.code == "START_UNCONFIRMED"
    assert len(fake_server.refresh_posts) == 1


@pytest.mark.parametrize("fake_server", ["iframe"], indirect=True)
def test_headless_refreshes_the_legacy_table_inside_an_iframe(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    result = asyncio.run(refresh_table(settings_for(fake_server, tmp_path), "users"))
    assert result.status == "completed"
    assert result.refresh_id == "101"
    assert result.replication_id == "100001"
    assert len(fake_server.logins) == 1
    assert len(fake_server.refresh_posts) == 1


@pytest.mark.parametrize("fake_server", ["delayed"], indirect=True)
def test_headless_polls_when_the_post_does_not_yet_identify_the_new_task(browser_available, fake_server, tmp_path, monkeypatch):
    monkeypatch.setenv("QB_USERNAME", "local@example.test")
    monkeypatch.setenv("QB_PASSWORD", "local-test-password")
    result = asyncio.run(refresh_table(settings_for(fake_server, tmp_path), "users"))
    assert result.status == "completed"
    assert result.refresh_id == "101"
    assert len(fake_server.refresh_posts) == 1
    assert fake_server.polls_after_start >= 2
