import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager

import httpx


@contextmanager
def listener(app, env):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app, "--fd", str(sock.fileno()), "--log-level", "warning"],
            env=env, pass_fds=(sock.fileno(),),
        )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 5
            while True:
                try:
                    client.get("/openapi.json")
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise
                    time.sleep(0.05)
            yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_public_fake_ai_and_authenticated_managers_use_separate_listeners(tmp_path):
    common = dict(os.environ, BUSINESS_STATE_FILE=str(tmp_path / "world.sqlite"),
                  HONEYNET_COWRIE_EXPORT=str(tmp_path / "ssh" / "world.sqlite"),
                  RESEARCH_STATE_FILE=str(tmp_path / "research.sqlite"),
                  HONEYNET_LATENCY_FILE=str(tmp_path / "http.sqlite"), HONEYNET_PG_DSN="")
    for name in ("RESEARCH_MODEL_API_KEY", "HONEYNET_CONTROL_TOKEN", "HONEYNET_DASHBOARD_PASSWORD"):
        common.pop(name, None)
    with ExitStack() as stack:
        public = stack.enter_context(listener("business.app:app", dict(common, HONEYNET_VISITOR_ONLY="1")))
        control = stack.enter_context(listener("research.control_app:app", dict(common, HONEYNET_CONTROL_TOKEN="test-control")))
        dashboard = stack.enter_context(listener("management.app:app", dict(common, HONEYNET_DASHBOARD_PASSWORD="test-dashboard")))
        headers = {"x-control-token": "test-control"}
        assert public.get("/api/research/status", headers=headers).status_code == 404
        assert control.get("/api/research/status").status_code == 404
        assert control.get("/api/research/status", headers=headers).json()["model_configured"] is False
        response = public.post("/v1/chat/completions", headers={
            "Authorization": "Bearer sk-demo-internal-2026", "X-Session": "loopback",
        }, json={"model": "corp-llm-7b", "messages": [{"role": "user", "content": "内网资产"}]})
        assert response.status_code == 200
        assert "web01" in response.json()["choices"][0]["message"]["content"]
        assert "x-deception" not in response.headers
        assert control.get("/v1/models").status_code == 404
        assert public.get("/api/runtime").status_code == 404
        assert dashboard.get("/api/runtime").status_code == 401
        data = dashboard.get("/api/runtime", auth=("operator", "test-dashboard")).json()
        assert data["events_by_entry"]["llm"] == 1
        assert data["http_response_p95_ms"] > 0
        operator = ("operator", "test-dashboard")
        assert public.get("/api/templates").status_code == 404
        assert dashboard.get("/templates").status_code == 401
        assert dashboard.get("/templates", auth=operator).status_code == 200
        template = dashboard.post("/api/templates", auth=operator, json={
            "name": "回环验收", "variant": "agent_note", "goal": "验证合成运维场景",
        })
        assert template.status_code == 200
        saved = template.json()
        run = control.post("/api/research/runs", headers=headers, json={
            "mode": "fixture", "template_id": saved["template_id"], "template_revision": saved["revision"],
        })
        assert run.status_code == 200
        assert public.get(run.json()["entry"]).status_code == 200
