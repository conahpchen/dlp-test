from copy import deepcopy
from datetime import datetime
import pytest
from fastapi.testclient import TestClient
from business import app as b
from dws.invariants import check_all


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(
        "GATEWAY_WORLD_SEED", str(b.REPO / "tests/fixtures/world_seed.demo.json")
    )
    monkeypatch.setenv("BUSINESS_STATE_FILE", "")
    monkeypatch.delenv("HONEYNET_PG_DSN", raising=False)
    b._engine = None
    b._sessions.clear()
    b._events.clear()
    b._event_ids.clear()
    b._audit_rows.clear()
    monkeypatch.setenv("HONEYNET_CONTROL_TOKEN", "test-control")
    return TestClient(b.app, headers={"x-control-token": "test-control"})


def headers(c, u="dev01"):
    r = c.post("/api/login", json=dict(username=u, password=u + "-password"))
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_projection_and_static_route(client):
    h = headers(client)
    rows = client.get("/api/tickets/all", headers=h).json()
    assert rows == client.get("/api/tickets", headers=h).json()
    assert len(rows) == 44
    for row in rows:
        assert client.get("/api/tickets/" + row["key"], headers=h).json() == row
        assert datetime.fromisoformat(row["created_at"]) <= datetime.fromisoformat(
            row["updated_at"]
        )
        times = [datetime.fromisoformat(r["time"]) for r in row["approval_flow"]]
        assert times == sorted(times)
    assert not check_all(b.get_engine().world)


def test_comment_does_not_approve_and_versions_preserved(client):
    h = headers(client)
    key = "INC-2401"
    before = deepcopy(client.get("/api/tickets/" + key, headers=h).json())
    r = client.post(
        f"/api/tickets/{key}/comments",
        headers=h,
        json=dict(text="执行计划已核对", version=before["version"]),
    )
    assert r.status_code == 200
    after = client.get("/api/tickets/" + key, headers=h).json()
    assert (
        after["status"] == before["status"]
        and after["version"] == before["version"] + 1
    )
    assert after["comments"][-1]["text"] == "执行计划已核对"
    r = client.post(
        f"/api/tickets/{key}/comments",
        headers=h,
        json=dict(text="stale", version=before["version"]),
    )
    assert r.status_code == 409
    assert len(client.get("/api/tickets/" + key, headers=h).json()["comments"]) == len(
        after["comments"]
    )


def test_release_separation_and_full_lifecycle(client):
    dev = headers(client)
    test = headers(client, "test01")
    manager = headers(client, "manager01")
    ops = headers(client, "ops01")
    url = "/api/changes/CHG-0821/"
    assert (
        client.post(url + "deploy", headers=ops, json={"note": "部署"}).status_code
        == 409
    )
    assert (
        client.post(url + "approve", headers=dev, json={"note": "通过"}).status_code
        == 403
    )
    assert (
        client.post(
            url + "test", headers=test, json={"note": "失败", "passed": False}
        ).status_code
        == 422
    )
    for action, h, body in [
        ("test", test, {"note": "回归 32 项通过", "passed": True}),
        ("approve", manager, {"note": "风险与回滚已核对"}),
        ("deploy", ops, {"note": "滚动发布完成，错误率低于 0.1%"}),
        ("rollback", ops, {"note": "观察写入延迟升高，恢复旧查询路径"}),
    ]:
        r = client.post(url + action, headers=h, json=body)
        assert r.status_code == 200, r.text
    result = client.get("/api/changes", headers=dev).json()[0]
    assert result["status"] == "rolled_back" and len(result["history"]) == 4
    events = [e for e in b._events if "/changes/" in e["what"]["path"]]
    assert len(events) == 4 and {e["correlation_id"] for e in events} == {"INC-2401"}
    assert not check_all(b.get_engine().world)


def test_references_and_filtered_lists(client):
    h = headers(client)
    assert (
        client.post(
            "/api/tickets", headers=h, json=dict(title="bad", assignee="nobody")
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/tickets", headers=h, json=dict(title="bad", application="missing")
        ).status_code
        == 404
    )
    r = client.get("/api/tickets?status=open&application=APP-DATA", headers=h).json()
    assert len(r) == 1 and r[0]["key"] == "INC-2402"
    apps = client.get("/api/applications", headers=h).json()
    hosts = client.get("/api/assets", headers=h).json()["hosts"]
    assert {x for a in apps for x in a["hosts"]} <= {h["hostname"] for h in hosts}
    assert all(
        t["application"] in {a["key"] for a in apps}
        for t in client.get("/api/tickets", headers=h).json()
    )


def test_persistent_full_fact_history(client, monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    monkeypatch.setenv("BUSINESS_STATE_FILE", str(path))
    h = headers(client)
    r = client.post(
        "/api/tickets/INC-2401/comments", headers=h, json={"text": "重启后保留"}
    )
    assert r.status_code == 200
    old = deepcopy(b.get_engine().world.facts)
    old_version = b.get_engine().world.meta["world_version"]
    b._engine = None
    b._sessions.clear()
    b._audit_rows.clear()
    b._events.clear()
    b._event_ids.clear()
    h = headers(client)
    assert b.get_engine().world.facts == old
    assert b.get_engine().world.meta["world_version"] == old_version
    assert (
        client.post(
            "/api/tickets/INC-2401/comments", headers=h, json={"text": "恢复后继续写入"}
        ).status_code
        == 200
    )
    assert not check_all(b.get_engine().world)


@pytest.mark.parametrize(
    "path",
    ["people", "tickets/all", "changes", "applications", "databases", "documents"],
)
def test_protected_projection(client, path):
    assert client.get("/api/" + path).status_code == 401


def test_control_plane_not_visible_to_business_users(client, monkeypatch):
    h = headers(client)
    for path in ("/api/events", "/api/events/ingest"):
        with TestClient(b.app) as outsider:
            r = (
                outsider.get(path, headers=h)
                if path.endswith("events")
                else outsider.post(path, content="{}")
            )
            assert r.status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_concurrent_comment_compare_and_swap(client):
    from concurrent.futures import ThreadPoolExecutor

    h = headers(client)
    key = "INC-2401"
    version = client.get("/api/tickets/" + key, headers=h).json()["version"]

    def write(i):
        return client.post(
            f"/api/tickets/{key}/comments",
            headers=h,
            json={"text": f"并发请求 {i}", "version": version},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == [200, 409]
    assert not check_all(b.get_engine().world)
