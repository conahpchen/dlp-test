import asyncio
import json
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


def test_slow_model_read_timeout_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    monkeypatch.setenv("RESEARCH_MODEL_TIMEOUT_SECONDS", "600")

    def transport(request):
        assert request.extensions["timeout"]["read"] == 600
        assert request.extensions["timeout"]["connect"] == 15
        return provider_ok('{"accepted": true}')

    model = Model(httpx.MockTransport(transport), budget=ModelBudget(tmp_path / "budget.sqlite", daily_tokens=20000))
    assert model.choose([]) == {"accepted": True}
    monkeypatch.setenv("RESEARCH_MODEL_TIMEOUT_SECONDS", "601")
    with pytest.raises(ValueError, match="between 1 and 600"):
        Model()


def test_slow_template_generation_keeps_its_lease_through_multiple_model_calls(tmp_path, monkeypatch):
    from research import template_scenes
    from research.lab import ResearchLab

    lab = ResearchLab(tmp_path / "research.sqlite")
    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    monkeypatch.setenv("RESEARCH_MODEL_TIMEOUT_SECONDS", "600")
    now = [100]
    monkeypatch.setattr(template_scenes, "time", lambda: now[0])

    def generate(lab, provider, model, scene_job, template_id, revision):
        assert model.read_timeout == 600
        for _ in range(3 * model.total_timeout // template_scenes.HEARTBEAT_SECONDS):
            now[0] += template_scenes.HEARTBEAT_SECONDS
            assert template_scenes._renew_lease(lab, scene_job)
        assert not template_scenes.process_one(lab, provider)
        return "model_timeout"

    monkeypatch.setattr(template_scenes, "_generate", generate)
    template_scenes.enqueue(lab, "operations", 1)
    assert template_scenes.process_one(lab, None)
    assert template_scenes.status(lab, "operations", 1)["error"] == "model_timeout"


def test_template_scene_progress_migrates_a_preexisting_queue(tmp_path):
    from research.lab import ResearchLab
    from research.template_scenes import status

    ledger = tmp_path / "research.sqlite"
    with closing(sqlite3.connect(ledger)) as db, db:
        db.execute("CREATE TABLE template_scene_jobs (template_id TEXT, revision INTEGER, id TEXT, "
                   "status TEXT, lease TEXT, leased_until INTEGER, error TEXT, PRIMARY KEY(template_id,revision))")
        db.execute("INSERT INTO template_scene_jobs VALUES ('operations',1,'old-id','ready',NULL,NULL,NULL)")
    result = status(ResearchLab(ledger), "operations", 1)
    assert result["status"] == "ready" and result["run_id"] == "old-id"
    assert result["stage"] is None and result["elapsed_seconds"] is None


def test_new_session_activity_waits_for_an_existing_publication(tmp_path, monkeypatch):
    from research import worker
    from research.lab import ResearchLab

    lab = ResearchLab(tmp_path / "research.sqlite")
    now = [100]
    monkeypatch.setattr(worker, "time", lambda: now[0])
    states = iter([{"status": "publishing", "waiting_on_release": True}, {"status": "applied"}])
    monkeypatch.setattr(worker, "adapt", lambda *args, **kwargs: next(states))
    assert worker.enqueue(lab, "run", "session")
    assert worker.process_one(lab, object())
    assert worker.result_status(lab, "run", "session") == "pending"
    assert not worker.process_one(lab, object())
    now[0] += worker.PUBLISH_RETRY_SECONDS
    assert worker.process_one(lab, object())
    assert not worker.process_one(lab, object())


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
    with pytest.raises(InferenceUnavailable, match="cooling down") as denied:
        model.choose([])
    assert denied.value.code == "model_circuit_open"
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
    with lab.connect(write=False) as db:
        decision = json.loads(db.execute("SELECT payload FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert [attempt["code"] for attempt in decision["attempts"]] == ["model_budget_tokens"]
    assert client.get(root + "/interact").status_code == 503
    assert client.get(root + "/index").status_code == 200
    assert client.post(root + "/interact", json={"message": "查看数据库备份"}).status_code == 202
    assert process_one(lab, InputModel())
    assert "database_backup" in client.get(root + "/interact").json()["content"]


def test_automatic_decoys_generate_and_deliver_without_template(demo_seed, monkeypatch, tmp_path):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dws.world import World
    from replay.engine import Engine
    from research import control_app
    from research.api import current_lab, install_routes
    from research.worker import process_one
    from tests.test_generative_research import SceneModel

    monkeypatch.setenv("RESEARCH_STATE_FILE", str(tmp_path / "research.sqlite"))
    monkeypatch.setenv("RESEARCH_AUTOMATIC_DECOYS", "1")
    monkeypatch.setenv("RESEARCH_ADAPTIVE_ENABLED", "1")
    monkeypatch.delenv("HONEYNET_VISITOR_HEALTH_PORT", raising=False)
    engine = Engine(World(demo_seed))
    class BusinessModel(SceneModel):
        def choose(self, messages):
            if "short original Chinese operations note" in messages[0]["content"]:
                self.calls.append(messages)
                return dict(intent="knowledge review", title="文档维护检查", body="核对文档的维护职责，并记录仍需确认的业务说明。")
            return super().choose(messages)

    model = BusinessModel()
    monkeypatch.setattr(control_app, "checkpoint", lambda: (engine, {}))
    monkeypatch.setattr("research.model.Model", lambda: model)

    async def start():
        await asyncio.wait_for(control_app.bootstrap_scene(asyncio.Event()), timeout=2)

    lab = current_lab()
    assert lab.active() is None
    asyncio.run(start())
    run = lab.active()
    assert lab.plan(run)["origin"] == "ai"
    assert "template" not in lab.plan(run)
    asyncio.run(start())
    assert lab.active() == run
    assert len(model.calls) == 2

    visitor = FastAPI()
    visitor.get("/")(lambda: {"page": "OA"})
    visitor.get("/api/documents")(lambda: [{"title": "研发报表"}])
    install_routes(visitor, lambda: engine, surface="visitor")
    client = TestClient(visitor)
    assert client.get("/").json() == {"page": "OA"}
    assert "link" not in client.get("/api/documents").headers
    assert len(model.calls) == 2
    assert process_one(lab, model)
    response = client.get("/api/documents")
    expected_link = f"/decoy/{run}/scene/oa-guide"
    assert response.headers["link"] == f'<{expected_link}>; rel="related"'
    assert client.get(expected_link).status_code == 200
    assert lab.report(run)["decisions"][0]["decision"]["intent"] == "knowledge review"


class ModelStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False):
        self.chunks = chunks
        self.stall = stall

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
        if self.stall:
            await asyncio.sleep(10)


def sse_message(value):
    return b"data: " + json.dumps(value).encode() + b"\r\n\r\n"


def test_stream_drops_reasoning_reconciles_usage_and_reports_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL_BASE_URL", "https://synthetic-model.example/v1")
    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    monkeypatch.setenv("RESEARCH_MODEL_THINKING", "disabled")
    monkeypatch.setenv("RESEARCH_MODEL_THINKING_FIELD", "chat_template_kwargs")
    monkeypatch.setenv("RESEARCH_MODEL_STREAM", "1")
    ledger = tmp_path / "budget.sqlite"
    first = sse_message({"choices": [{"delta": {"reasoning_content": "discard me"}}]})
    chunks = [
        first[:12], first[12:],
        sse_message({"choices": [{"delta": {"content": '{"accepted":'}}]}),
        sse_message({"choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}]}),
        sse_message({"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                                              "completion_tokens_details": {"reasoning_tokens": 12}}}),
        b"data: [DONE]\n\n",
    ]

    def transport(request):
        body = json.loads(request.content)
        assert body["stream"] and body["stream_options"]["include_usage"]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["max_tokens"] == 800
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ModelStream(chunks))

    model = Model(httpx.MockTransport(transport), budget=ModelBudget(ledger, daily_tokens=25_000))
    stages = []
    model.progress = stages.append
    for prompt in ("same", "same", "another"):
        assert model.choose_for_phase([{"role": "user", "content": prompt}], "review") == {"accepted": True}
    assert stages == ["review_waiting", "review_reasoning", "review_receiving"] * 2
    assert "discard me" not in str(model.last_response_metadata)
    with closing(sqlite3.connect(ledger)) as db:
        assert db.execute("SELECT tokens FROM daily_budget").fetchone() == (300,)


@pytest.mark.parametrize(("chunks", "stall", "expected"), [
    ([httpx.ReadTimeout("synthetic")], False, "model_first_response_timeout"),
    ([sse_message({"choices": [{"delta": {"reasoning_content": "ignore"}}]}),
      httpx.ReadTimeout("synthetic")], False, "model_response_idle"),
    ([sse_message({"choices": [{"delta": {"reasoning_content": "ignore"}}]})], True, "model_deadline"),
])
def test_stream_distinguishes_timeout_phases(tmp_path, monkeypatch, chunks, stall, expected):
    from research.model import ModelTimeout

    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    monkeypatch.setenv("RESEARCH_MODEL_STREAM", "1")

    def transport(_request):
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ModelStream(chunks, stall=stall))

    model = Model(httpx.MockTransport(transport), budget=ModelBudget(tmp_path / "budget.sqlite", 30_000))
    model.total_timeout = .05
    with pytest.raises(ModelTimeout) as failure:
        model.choose([])
    assert failure.value.code == expected


def test_auth_failure_does_not_trip_transient_circuit_and_budget_denial_is_typed(tmp_path, monkeypatch):
    from research.model import ModelProviderError

    monkeypatch.setenv("RESEARCH_MODEL_API_KEY", "synthetic")
    budget = ModelBudget(tmp_path / "budget.sqlite", 19_000, failures=1)
    attempts = []

    def transport(request):
        attempts.append(request)
        return httpx.Response(401)

    model = Model(httpx.MockTransport(transport), budget=budget)
    for _ in range(3):
        with pytest.raises(ModelProviderError, match="401"):
            model.choose([])
    with pytest.raises(InferenceUnavailable) as denied:
        model.choose([])
    assert denied.value.code == "model_budget_tokens" and len(attempts) == 3
    assert budget.open_until == 0
