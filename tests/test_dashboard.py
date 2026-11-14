"""Dashboard auth: every /api route requires the env-backed X-Shield-Auth token."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard

API_ROUTES = {
    ("GET", "/api/status"),
    ("POST", "/api/mode"),
    ("POST", "/api/trade"),
    ("POST", "/api/ai-scan"),
    ("POST", "/api/card/{token}/{action}"),
    ("POST", "/api/kill"),
}
ROOT = Path(__file__).resolve().parents[1]


def test_route_inventory_is_fully_covered():
    """Fail if a new /api route is added without adding it to the auth test list."""
    actual = {
        (method, route.path)
        for route in dashboard.app.routes
        if getattr(route, "path", "").startswith("/api/")
        for method in (route.methods or set())
        if method != "HEAD"
    }
    assert actual == API_ROUTES


@pytest.mark.parametrize("method,path", sorted(API_ROUTES))
@pytest.mark.parametrize(
    "headers", [{}, {"x-shield-auth": "wrong"}], ids=["missing", "wrong"]
)
def test_every_api_route_returns_401_without_valid_token(method, path, headers):
    response = TestClient(dashboard.app).request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_correct_token_is_accepted():
    response = TestClient(dashboard.app).get(
        "/api/status", headers={"X-Shield-Auth": dashboard.SHIELD_AUTH_TOKEN}
    )
    assert response.status_code == 200


def test_resolve_keys_does_not_fall_back_to_server_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "server-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "server-secret")
    with pytest.raises(dashboard.HTTPException) as exc:
        dashboard.resolve_keys("", "", "", "", "")
    assert exc.value.status_code == 401


def test_dashboard_refuses_to_start_without_auth_token():
    env = os.environ.copy()
    env["SHIELD_AUTH_TOKEN"] = ""
    result = subprocess.run(
        [sys.executable, "-c", "import dashboard"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SHIELD_AUTH_TOKEN" in result.stderr
