"""Opt-in full MySQL container release validation."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pymysql
import pytest

from dws.world import World
from research.isolated_mysql import ready, stop
from research.lab import ResearchLab
from tests.test_generative_research import SceneModel


@pytest.mark.skipif(not os.environ.get("HONEYNET_TEST_L3_MYSQL_IMAGE"), reason="requires approved local MySQL image")
def test_l3_mysql_isolated_release_queries_and_cleans_up(demo_seed, tmp_path, monkeypatch):
    image = os.environ["HONEYNET_TEST_L3_MYSQL_IMAGE"]
    monkeypatch.setenv("HONEYNET_L3_MYSQL_IMAGE", image)
    lab = ResearchLab(tmp_path / "research.sqlite")
    world = World(demo_seed)
    original = lab.create(world, "plain", model=SceneModel())["run_id"]
    run = lab.create(world, "plain", model=SceneModel())["run_id"]
    plan = lab.plan(run)
    plan["scene"]["capability_requests"] = ["isolated_instance"]
    plan["scene"]["isolated_reason"] = "sql_transactions"
    with lab.connect() as db:
        db.execute("UPDATE runs SET plan=? WHERE id=?", (json.dumps(plan), run))
    lab.activate(original)
    assert lab.active_for("old") == original
    with tempfile.TemporaryDirectory(prefix="dlp-l3-", dir="/tmp") as directory:
        monkeypatch.setenv("HONEYNET_L3_STATE_DIR", directory)
        instance = Path(directory) / run
        name = "honeynet-l3-" + run
        try:
            with pytest.raises(ValueError, match="No approved"):
                lab.activate(run)
            assert lab.active() == original
            with monkeypatch.context() as patch:
                patch.setenv("HONEYNET_L3_MYSQL_IMAGE", "mysql@sha256:" + "0" * 64)
                with pytest.raises(ValueError, match="Docker operation"):
                    lab.activate(run, approve_isolated=True)
                assert lab.active() == original
                assert not instance.exists()
            assert lab.activate(run, approve_isolated=True)["active_run"] == run
            assert lab.active_for("old") == original
            assert lab.active_for("new") == run
            status = lab.release_status(run)
            assert status["manifest"]["capabilities"][-1]["level"] == 3
            inspected = json.loads(subprocess.check_output(["docker", "inspect", name], text=True, timeout=10))[0]
            assert inspected["HostConfig"]["NetworkMode"] == "none"
            assert not inspected["HostConfig"]["PortBindings"]
            assert inspected["HostConfig"]["ReadonlyRootfs"]
            assert inspected["HostConfig"]["Memory"] == 512 * 1024 * 1024
            assert ready(run)
            password = (instance / "catalog.password").read_text()
            with pymysql.connect(unix_socket=str(instance / "socket" / "mysql.sock"), user="catalog_reader",
                                 password=password, database="collab_core", read_timeout=3) as db, db.cursor() as cursor:
                cursor.execute("SELECT c.name, count(*) FROM catalog c JOIN catalog_records r "
                               "ON c.id=r.catalog_id GROUP BY c.name")
                assert cursor.fetchall() == ((plan["documents"][0]["path"], 2),)
                cursor.execute("SELECT world_version FROM projection")
                assert cursor.fetchone() == (plan["world_version"],)
                cursor.execute("INSERT INTO catalog_records VALUES (3,1,'temporary')")
                db.rollback()
                cursor.execute("SELECT COUNT(*) FROM catalog_records")
                assert cursor.fetchone() == (2,)
            subprocess.run(["docker", "stop", name], check=True, capture_output=True, timeout=20)
            assert lab.compensate_unready() == {"status": "compensated", "active_run": original}
            assert lab.active_for("later") == original
            assert not instance.exists()
            assert lab.release_status(run)["attempts"][0]["status"] == "compensated"
            assert lab.activate(run, approve_isolated=True)["active_run"] == run
            assert lab.rollback(run)["active_run"] == original
            assert not instance.exists()
        finally:
            stop(run)
