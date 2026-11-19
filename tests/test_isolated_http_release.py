import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from dws.world import World
from research import isolated_http
from research.lab import ResearchLab
from tests.test_generative_research import SceneModel


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_L3_HTTP_IMAGE"), reason="requires approved local Nginx image")
def test_l3_http_real_socket_and_rollback(demo_seed, tmp_path, monkeypatch):
    monkeypatch.setenv("HONEYNET_L3_HTTP_IMAGE", os.environ["HONEYNET_TEST_L3_HTTP_IMAGE"])
    lab = ResearchLab(tmp_path / "research.sqlite")
    world = World(demo_seed)
    previous = lab.create(world, "plain", model=SceneModel())["run_id"]
    run = lab.create(world, "plain", model=SceneModel())["run_id"]
    plan = lab.plan(run)
    plan["scene"].update(capability_requests=["isolated_instance"], isolated_reason="standalone_http")
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))
    lab.activate(previous)
    with tempfile.TemporaryDirectory(prefix="dlp-http-", dir="/tmp") as directory:
        monkeypatch.setenv("HONEYNET_L3_STATE_DIR", directory)
        try:
            with pytest.raises(ValueError, match="No approved"):
                lab.activate(run)
            assert lab.active() == previous
            assert lab.activate(run, approve_isolated=True)["active_run"] == run
            manifest = lab.release_status(run)["manifest"]
            assert manifest["capabilities"][-1]["name"] == "isolated_http"
            socket_path = Path(directory) / run / "socket" / "http.sock"
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path))) as client:
                response = client.get("http://local/index.html")
                assert response.status_code == 200
                assert plan["documents"][0]["path"] in response.text
                assert client.get("http://local/other").status_code == 404
            subprocess.run(["docker", "stop", "honeynet-l3-" + run], check=True, capture_output=True, timeout=25)
            assert lab.compensate_unready() == {"status": "compensated", "active_run": previous}
            assert not socket_path.exists()
        finally:
            isolated_http.stop(run)
