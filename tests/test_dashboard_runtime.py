from fastapi.testclient import TestClient

from business import app as business
from management.app import app as management


def test_management_listener_displays_measured_public_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("HONEYNET_LATENCY_FILE", str(tmp_path / "latency.sqlite"))
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(tmp_path / "checkpoint.json"))
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("HONEYNET_DASHBOARD_PASSWORD", "synthetic-test")
    monkeypatch.setattr(business, "_engine", None)
    public = TestClient(business.app)
    dashboard = TestClient(management)
    assert public.get("/api/runtime").status_code == 404
    assert public.get("/v1/models").status_code == 200
    assert dashboard.get("/api/runtime").status_code == 401
    data = dashboard.get("/api/runtime", auth=("operator", "synthetic-test")).json()
    assert data["http_sample_count"] == 2
    assert data["http_response_p95_ms"] > 0
    assert data["world_version"] == 1
    assert data["projected_services"]
    assert dashboard.get("/", auth=("operator", "synthetic-test")).status_code == 200
