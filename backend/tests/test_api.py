"""
Tests for Agent Backend
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


@pytest.fixture
def client():
    """Test client fixture."""
    return TestClient(app)


class TestHealth:
    """Health endpoint tests."""

    def test_health_check(self, client):
        """Test health endpoint returns healthy status."""
        response = client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "healthy"
        assert "service" in data
        assert "version" in data


class TestAPI:
    """API endpoint tests."""

    def test_docs_available(self, client):
        """Test OpenAPI docs are available."""
        response = client.get("/api/agent/docs")
        # Should return HTML or redirect
        assert response.status_code in [200, 307]

    def test_openapi_schema(self, client):
        """Test OpenAPI schema is generated."""
        response = client.get("/api/agent/openapi.json")
        assert response.status_code == 200

        schema = response.json()
        assert "openapi" in schema
        assert "paths" in schema


class TestLinkRoutesRequireAuth:
    """Guards the `Depends(get_current_user)` wiring on the link-management
    routes, not their logic.

    Every test in test_link_routes.py overrides get_current_user via
    app.dependency_overrides — correct for testing route logic, but it
    bypasses authentication entirely, so nothing there would catch a route
    silently losing its auth dependency. This test drives the real,
    unmodified app with no override: a request missing the gateway headers
    never reaches route code, so a plain synchronous TestClient is fine here
    (unlike test_link_routes.py, which needs the async client + db_pool to
    share one event loop for real database work). Do not "consolidate" this
    into the overridden tests — that removes exactly what it exists to catch.
    """

    def test_no_gateway_headers_is_rejected(self, client):
        response = client.get("/api/agent/links")
        assert response.status_code == 401


class TestInternal:
    """/internal/* routes — authenticated by X-Internal-Service-Secret, not gateway headers."""

    @pytest.fixture(autouse=True)
    def _internal_secret(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SERVICE_SECRET", "test-internal-secret")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_internal_rejects_missing_secret(self, client):
        response = client.post("/api/agent/internal/ping")
        assert response.status_code == 401

    def test_internal_rejects_wrong_secret(self, client):
        response = client.post(
            "/api/agent/internal/ping",
            headers={"X-Internal-Service-Secret": "wrong"},
        )
        assert response.status_code == 401

    def test_internal_accepts_correct_secret(self, client):
        response = client.post(
            "/api/agent/internal/ping",
            headers={"X-Internal-Service-Secret": "test-internal-secret"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
