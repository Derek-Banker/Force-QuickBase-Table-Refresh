"""Check refresh identity, upstream errors, and configuration boundaries."""

import asyncio
import json

import pytest

from qb_refresh import (
    RefreshError,
    RefreshSettings,
    RefreshTracker,
    known_tasks,
    refresh_request_id,
    response_payload,
    safe_failure,
    session_lock,
)


def task(identifier, *, status=3, replication_id="100001"):
    """Build a synthetic task without a browser or Quickbase account."""
    return {"id": identifier, "status": status, "replicationId": replication_id}


def test_old_success_is_not_completion_of_a_new_refresh():
    old = task(100)
    tracker = RefreshTracker("100001", known_tasks({"lastRefresh": old}))

    assert tracker.observe({
        "currentRefresh": task(101, status=1),
        "lastRefresh": old,
        "lastGoodRefresh": old,
    }) is None
    assert tracker.expected == "id:101"
    assert tracker.observe({"currentRefresh": None, "lastRefresh": old}) is None

    completed = task(101)
    assert tracker.observe({"currentRefresh": None, "lastRefresh": completed}) == completed


def test_refresh_can_finish_before_the_first_response():
    tracker = RefreshTracker("100001", {"id:100"})
    completed = task(101)
    assert tracker.observe({"currentRefresh": None, "lastRefresh": completed}) == completed


def test_old_error_does_not_report_a_new_refresh_failure():
    old_error = "CAPTCHA evaluation failed on the previous refresh"
    tracker = RefreshTracker("100001", {"id:100"}, baseline_error=old_error)
    assert tracker.observe({
        "currentRefresh": None,
        "lastRefresh": task(100),
        "lastError": old_error,
    }) is None
    assert tracker.expected is None


def test_matching_failed_task_is_not_reported_as_success():
    tracker = RefreshTracker("100001", {"id:100"})
    assert tracker.observe({"currentRefresh": task(101, status=1)}) is None
    with pytest.raises(RefreshError) as raised:
        tracker.observe({
            "currentRefresh": None,
            "lastRefresh": {**task(101, status=4), "endTime": "2026-10-15T00:00:00Z", "message": "CAPTCHA evaluation failed"},
        })
    assert raised.value.code == "CAPTCHA_FAILED"


def test_status_for_another_table_is_rejected():
    tracker = RefreshTracker("100001", {"id:100"})
    with pytest.raises(RefreshError) as raised:
        tracker.observe({"currentRefresh": task(101, status=1, replication_id="100002")})
    assert raised.value.code == "UNEXPECTED_RESPONSE"


class FakeResponse:
    """Provide only the response fields that the JSON validator reads."""

    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status

    async def json(self):
        return self.payload


def test_http_200_does_not_hide_a_captcha_error():
    response = FakeResponse({
        "currentRefresh": None,
        "lastError": "CAPTCHA evaluation failed: token=private-captcha-value",
    })
    payload = asyncio.run(response_payload(response))
    with pytest.raises(RefreshError) as raised:
        RefreshTracker("100001").observe(payload)
    assert raised.value.code == "CAPTCHA_FAILED"
    assert "private-captcha-value" not in str(raised.value)


@pytest.mark.parametrize("field", ["currentRefresh", "lastRefresh", "lastGoodRefresh"])
@pytest.mark.parametrize("value", ["private-upstream-value", [], False])
def test_malformed_tasks_return_a_controlled_error(field, value):
    response = FakeResponse({field: value})

    with pytest.raises(RefreshError) as raised:
        asyncio.run(response_payload(response))

    assert raised.value.code == "UNEXPECTED_RESPONSE"
    assert "private-upstream-value" not in raised.value.message


@pytest.mark.parametrize("message, expected_code", [
    ("CAPTCHA failed token=private-value", "CAPTCHA_FAILED"),
    ("Sign in required; cookie=private-value", "AUTHENTICATION_REQUIRED"),
    ("Unexpected https://example.test/?token=private-value", "REFRESH_FAILED"),
])
def test_upstream_errors_do_not_return_secrets(message, expected_code):
    error = safe_failure(message)
    assert error.code == expected_code
    assert "private-value" not in error.message
    assert "https://" not in error.message


@pytest.mark.parametrize("change", [
    {"realm_url": "http://example.quickbase.com"},
    {"realm_url": "https://quickbase.com.evil.example"},
    {"realm_url": "https://user:secret@example.quickbase.com"},
    {"url": "https://other.quickbase.com/nav/table/abc"},
    {"url": "https://example.quickbase.com/nav/table/abc?token=secret"},
    {"url": "https://example.quickbase.com/nav/table/abc?userToken=secret"},
    {"replication_id": "100001&token=secret"},
    {"poll_interval_seconds": 0},
    {"navigation_timeout_seconds": True},
    {"refresh_selector": ""},
    {"refresh_selector": "   "},
    {"refresh_selector": 123},
])
def test_configuration_rejects_unsafe_or_invalid_targets(tmp_path, change):
    config = {
        "realm_url": "https://example.quickbase.com",
        "tables": {"users": {"url": "https://example.quickbase.com/nav/table/abc"}},
    }
    for name, value in change.items():
        if name in {"url", "replication_id", "refresh_selector"}:
            config["tables"]["users"][name] = value
        else:
            config[name] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(RefreshError) as raised:
        RefreshSettings.from_file(path)
    assert raised.value.code == "CONFIG_ERROR"
    assert "secret" not in raised.value.message


@pytest.mark.parametrize("name", ["username_selector", "password_selector", "submit_selector"])
@pytest.mark.parametrize("value", [123, None, [], "", "   "])
def test_configuration_rejects_invalid_login_selectors(tmp_path, name, value):
    config = {
        "realm_url": "https://example.quickbase.com",
        "tables": {"target": {"url": "https://example.quickbase.com/nav/table/abc"}},
        name: value,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(RefreshError) as raised:
        RefreshSettings.from_file(path)

    assert raised.value.code == "CONFIG_ERROR"


def test_request_observer_only_accepts_the_configured_origin():
    realm = "https://example.quickbase.com"
    url = realm + "/qb/sync/refreshTasks?replicationID=100001&token=secret"
    assert refresh_request_id(url, realm, "POST") == "100001"
    assert refresh_request_id(realm + "/qb/sync/refreshTasks/100001", realm, "GET") == "100001"
    assert refresh_request_id(url.replace("example.quickbase", "other.quickbase"), realm, "POST") is None
    assert refresh_request_id(url, realm, "GET") is None


def test_session_lock_blocks_another_refresh(tmp_path):
    state = tmp_path / ".state" / "browser.json"
    with session_lock(state):
        with pytest.raises(RefreshError) as raised:
            with session_lock(state):
                pytest.fail("A second refresh obtained the same session lock.")
    assert raised.value.code == "BUSY"
    with session_lock(state):
        pass
