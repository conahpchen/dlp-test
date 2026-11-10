"""grounded 生成与引擎行为（契约 12 §8；13 §6 M0 验收路径）。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from replay.engine import Engine
from dws.world import World
from replay.harness import load_events
from replay.trajectories import generate

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run(demo_seed, events, n=None):
    world = World(demo_seed)
    engine = Engine(world)
    responses = [engine.process(e) for e in (events[:n] if n else events)]
    return world, engine, responses


def _resp(responses, index):
    return responses[index]


def test_full_demo_session(demo_seed, demo_events):
    world, engine, responses = _run(demo_seed, demo_events)
    assert len(responses) == len(demo_events)

    # 连接 banner 来自世界（grounding）
    r0 = responses[0]
    assert r0["status"] == "ok"
    assert "Welcome to Ubuntu 22.04.3 LTS" in r0["output"][0]

    # 两次暴破失败
    assert responses[1]["status"] == "auth_fail"
    assert responses[2]["status"] == "auth_fail"

    # deploy 登录成功（凭据 valid_on web01）
    assert responses[3]["status"] == "ok"
    assert world.sessions["demo-sess-1"].user == "deploy"
    # 登录成功时钉定世界版本（= 登录事件开始处理时的版本）
    assert world.sessions["demo-sess-1"].pinned_version == responses[3]["dws_version"]

    assert responses[4]["output"] == ["deploy"]                 # whoami
    assert "web01" in responses[5]["output"][0]                 # uname -a
    passwd = "\n".join(responses[6]["output"])
    assert "root:x:0:0" in passwd and "deploy:x:" in passwd  # /etc/passwd 由 identity 生成（uid 随身份集合变化）
    hosts = "\n".join(responses[7]["output"])
    assert "10.20.3.10\tweb01" in hosts                          # /etc/hosts 由 host 生成
    ls = "\n".join(responses[8]["output"])
    assert "notes.txt" in ls and "projects/" in ls               # 数据事实 + 目录
    assert "rotate ssh keys" in responses[9]["output"][0]        # cat 数据文件


def test_write_roundtrip_visible_to_session(demo_seed, demo_events):
    world, engine, responses = _run(demo_seed, demo_events)
    # echo > report.txt（提交事实）→ cat report.txt（自写叠加可见，契约 §5.1 v1.1）
    assert responses[10]["status"] == "ok"
    assert "incident report 47" in responses[11]["output"][0]
    # 世界里确实追加了新版本事实
    from dws.ids import entity_uuid
    rows = world.facts["data"][entity_uuid("data", "/home/deploy/report.txt")]
    assert len(rows) == 1 and rows[-1].version == 1


def test_network_grounded_by_relations(demo_seed, demo_events):
    _, _, responses = _run(demo_seed, demo_events)
    assert "3 packets transmitted, 3 received, 0% packet loss" in responses[12]["output"][-1]
    assert responses[13]["status"] == "error"                    # ping 未知主机
    assert "Name or service not found" in responses[13]["output"][0]
    ssh_pivot = responses[17]
    assert "Connection closed by" in "".join(ssh_pivot["output"])  # web01→db01 可达


def test_unknown_command_and_fallback(demo_seed, demo_events):
    world, engine, responses = _run(demo_seed, demo_events)
    # 未知命令 = 合法 grounded 模板（不是 fallback）
    assert responses[18]["output"] == ["bash: figlet: command not found"]
    assert responses[18]["by"] == "template"

    # 目标主机不在世界 → grounding 失败 → fail-closed 兜底
    events = copy.deepcopy(demo_events[:1])
    events[0]["dst"]["host"] = "ghost01"
    world2 = World(demo_seed)
    engine2 = Engine(world2)
    resp = engine2.process(events[0])
    assert resp["by"] == "template_fallback"
    assert engine2.metrics["fallbacks"] == 1


def test_budget_rejects_expansion_after_12_entities(demo_seed):
    """世界预算：单会话新增实体 > 12 → 拒绝扩张，fail-closed 不矛盾（契约 §2.8）。"""
    seed_dict, events = generate("budget-demo", 7)
    world = World(seed_dict)
    engine = Engine(world)
    statuses = [engine.process(e) for e in events]
    rejected = [r for r in statuses if r["status"] == "rejected"]
    # 只要该轨迹有足量写操作，第 13 个新实体必被拒绝
    writes = [e for e in events if e["what"]["kind"] == "ssh.command"
              and any(op in e["what"].get("cmd", "") for op in (">", "mkdir", "touch"))]
    if len(writes) > 12:
        assert rejected, "超预算写操作应被拒绝"


def test_leak_guard_blocks_undeclared_ip(demo_seed, demo_events):
    """泄漏守卫：响应中出现未声明网段 IP → 拦截走 fallback。"""
    world = World(demo_seed)
    engine = Engine(world)
    events = copy.deepcopy(demo_events)
    # 把 notes.txt 内容改成含未声明网段 IP 的文本，cat 时守卫应拦截
    from dws.ids import entity_uuid
    world.facts["data"][entity_uuid("data", "/home/deploy/notes.txt")][0].payload[
        "content"] = "server at 8.8.8.8\n"
    for ev in events[:10]:
        engine.process(ev)
    cat_resp = engine.process(events[9])  # cat /home/deploy/notes.txt
    assert cat_resp["by"] == "template_fallback"
