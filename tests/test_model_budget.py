import sqlite3
from contextlib import closing

import httpx
import pytest

from research.model import Model
from research.model_budget import InferenceUnavailable, ModelBudget


def provider_ok(value):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": value}, "finish_reason": "stop"}],
    })


def test_durable_token_reservations_and_exact_request_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    ledger = tmp_path / "budget.sqlite"
    budget = ModelBudget(ledger, daily_tokens=6200)
    seen = []

    def transport(request):
        seen.append(request.url.path)
        return provider_ok('{"synthetic": {"enabled": true}}')

    model = Model(httpx.MockTransport(transport), budget=budget)
    result = model.choose([])
    result["synthetic"]["enabled"] = False
    assert model.choose([])["synthetic"]["enabled"]
    assert len(seen) == 1

    restarted = Model(httpx.MockTransport(transport), budget=ModelBudget(ledger, daily_tokens=6200))
    with pytest.raises(InferenceUnavailable, match="token budget"):
        restarted.choose([])
    assert len(seen) == 1
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT tokens FROM daily_budget").fetchone() == (6130,)


def test_cost_cap_and_breaker_with_half_open_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    clock = [100.]
    monkeypatch.setattr("research.model_budget.monotonic", lambda: clock[0])
    budget = ModelBudget(tmp_path / "budget.sqlite", 60_000, 20_000, 1_000_000, 1_000_000,
                         failures=2, cooldown=30)
    responses = iter([httpx.Response(503), httpx.Response(503), provider_ok('{"recovered":true}')])
    attempts = []

    def transport(request):
        attempts.append(request.url.path)
        return next(responses)

    model = Model(httpx.MockTransport(transport), budget=budget)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="503"):
            model.choose([])
    with pytest.raises(InferenceUnavailable, match="cooling down"):
        model.choose([])
    assert len(attempts) == 2
    clock[0] += 30
    assert model.choose([]) == {"recovered": True}
    assert len(attempts) == 3
    with pytest.raises(InferenceUnavailable, match="cost budget"):
        model.choose([{"role": "system", "content": "another approved request"}])
    with closing(sqlite3.connect(budget.ledger)) as db:
        assert db.execute("SELECT tokens,microusd FROM daily_budget").fetchone() == (18_390, 18_390)


def test_separate_visitor_queues_classified_activity_and_serves_during_budget_denial(
    demo_seed, tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dws.world import World
    from replay.engine import Engine
    from research.api import current_lab, install_routes
    from research.worker import process_one
    from tests.test_generative_research import SceneModel
    from tests.test_input_generation import InputModel

    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("RESEARCH_ASYNC_ADAPTATION", "1")
    lab = current_lab()
    engine = Engine(World(demo_seed))
    run = lab.create(engine.world, "plain", model=SceneModel())
    public = FastAPI()
    install_routes(public, lambda: engine, surface="visitor")
    client = TestClient(public)
    root = "/decoy/" + run["run_id"]
    assert client.post(root + "/interact", json={"message": "unclassified private value"}).status_code == 503
    assert client.get(root + "/interact").status_code == 404
    for _ in range(2):
        response = client.post(root + "/interact", json={"message": "查看数据库备份"})
        assert response.status_code == 202
    assert client.get(root + "/interact").status_code == 202
    assert client.get(root + "/index").status_code == 200

    def unexpected_call(_request):
        pytest.fail("Provider must not be called when the token budget is exhausted")

    model = Model(httpx.MockTransport(unexpected_call),
                  budget=ModelBudget(tmp_path / "budget.sqlite", daily_tokens=0))
    assert process_one(lab, model)
    assert not process_one(lab, model)
    assert client.get(root + "/interact").status_code == 503
    assert client.get(root + "/index").status_code == 200
    assert client.post(root + "/interact", json={"message": "查看数据库备份"}).status_code == 202
    assert process_one(lab, InputModel())
    assert "database_backup" in client.get(root + "/interact").json()["content"]
