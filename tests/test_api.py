"""Check API authorization and safe refresh results without a real browser."""

import json

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import api_server
from qb_refresh import RefreshError, RefreshResult


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "realm_url": "https://example.quickbase.com",
        "tables": {"users": {"url": "https://example.quickbase.com/nav/table/abc"}},
    }), encoding="utf-8")
    monkeypatch.setenv("QB_REFRESH_API_KEY", "test-api-key")
    with TestClient(api_server.create_app(path)) as connected_client:
        yield connected_client


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
def test_api_rejects_unauthorized_refresh(client, monkeypatch, headers):
    async def forbidden_call(*args):
        pytest.fail("The API started a browser before authenticating the caller.")
    monkeypatch.setattr(api_server, "refresh_table", forbidden_call)
    response = client.post("/refresh/users", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHORIZED"


def test_api_rejects_unconfigured_tables(client):
    response = client.post("/refresh/arbitrary-table", headers={"Authorization": "Bearer test-api-key"})
    assert response.status_code == 404


def test_api_waits_for_the_refresh_result(client, monkeypatch):
    calls = []
    async def completed_refresh(settings, table_name):
        calls.append(table_name)
        return RefreshResult(table_name, "100001", "101", "start", "finish", 0.1)
    monkeypatch.setattr(api_server, "refresh_table", completed_refresh)
    response = client.post("/refresh/users", headers={"Authorization": "Bearer test-api-key"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["refresh_id"] == "101"
    assert calls == ["users"]


@pytest.mark.parametrize("code, status_code", [
    ("CAPTCHA_FAILED", 422),
    ("BUSY", 409),
    ("AUTHENTICATION_REQUIRED", 401),
    ("REFRESH_TIMEOUT", 504),
])
def test_api_reports_refresh_failure(client, monkeypatch, code, status_code):
    async def failed_refresh(*args):
        raise RefreshError(code, "Controlled error message.")
    monkeypatch.setattr(api_server, "refresh_table", failed_refresh)
    response = client.post("/refresh/users", headers={"Authorization": "Bearer test-api-key"})
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_health_does_not_expose_account_information(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
