"""标注工具（M2）：双人盲标、进度、导出与 κ 联动。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from experiments.labeling import app as labeling_app
from experiments.labeling.bundles import build_session_bundles


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LABELING_DB", str(tmp_path / "labels.db"))
    monkeypatch.setattr(labeling_app, "DB_PATH", str(tmp_path / "labels.db"))
    labeling_app._init_db()
    return TestClient(labeling_app.app)


def _bundle(item_id: str, seq: int) -> dict:
    return {"item_id": item_id, "system": "cowrie", "seq": seq,
            "session": item_id, "lines": [f"# 会话 {item_id}", "[ssh] $ whoami"]}


def test_import_next_label_flow(client):
    rows = "\n".join(json.dumps(_bundle(f"s{i}", i)) for i in range(3))
    r = client.post("/api/import", content=rows.encode("utf-8"))
    assert r.json() == {"imported": 3}

    # A 标注员：按序取下一条、提交 T
    nxt = client.get("/api/next?annotator=a").json()
    assert nxt["item_id"] == "s0" and nxt["bundle"][0].startswith("# 会话")
    r = client.post("/api/label", json={"item_id": "s0", "annotator": "a",
                                        "category": "T", "note": "跨网段"})
    assert r.json()["ok"]

    # A 的下一条跳过已标 → s1
    assert client.get("/api/next?annotator=a").json()["item_id"] == "s1"
    # B 的下一条仍是 s0（盲标：B 不受 A 影响）
    assert client.get("/api/next?annotator=b").json()["item_id"] == "s0"


def test_progress_and_export_with_kappa(client):
    rows = "\n".join(json.dumps(_bundle(f"s{i}", i)) for i in range(2))
    client.post("/api/import", content=rows.encode("utf-8"))
    # A: s0=T, s1=none；B: s0=T, s1=T（一处分歧）
    client.post("/api/label", json={"item_id": "s0", "annotator": "a", "category": "T"})
    client.post("/api/label", json={"item_id": "s1", "annotator": "a", "category": "none"})
    client.post("/api/label", json={"item_id": "s0", "annotator": "b", "category": "T"})
    client.post("/api/label", json={"item_id": "s1", "annotator": "b", "category": "T"})

    p = client.get("/api/progress").json()
    assert p["items"] == 2 and p["labeled"] == {"a": 2, "b": 2} and p["both"] == 2

    ex = client.get("/api/export").json()
    assert ex["labels"]["a"]["s0"]["category"] == "T"
    assert ex["kappa"]["n"] == 2 and ex["kappa"]["kappa"] is not None


def test_label_category_validation(client):
    client.post("/api/import", content=json.dumps(_bundle("s0", 0)).encode("utf-8"))
    r = client.post("/api/label", json={"item_id": "s0", "annotator": "a",
                                        "category": "X"})
    assert r.status_code == 400
    r = client.post("/api/label", json={"item_id": "s0", "annotator": "c",
                                        "category": "T"})
    assert r.status_code == 400


def test_build_session_bundles_from_replay(demo_seed, demo_events):
    """从真实回放产物生成转写：一个会话一个 bundle，行内容可读。"""
    from replay.harness import run_replay
    responses, _, _, _ = run_replay(demo_seed, demo_events)
    bundles = build_session_bundles(demo_events, responses, system="cowrie")
    assert len(bundles) == 1  # demo 只有一个会话
    text = "\n".join(bundles[0]["lines"])
    assert "[ssh@198.51.100.0/24] $ whoami" in text
    assert "-> ok: deploy" in text or "-> ok" in text
