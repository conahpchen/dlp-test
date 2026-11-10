"""G1 演示：SSH→LLM pivot + 同一攻击者 + CSVR 非零（11 §04 M1 验收）。

    python -m experiments.pivot_demo

三个结论一次演示：
1. pivot 打通——攻击者在 SSH 会话里 cat notes.txt 拿到内部 API key，随后用它打 AI 网关；
2. 世界一致——两个入口的 attacker_id 相同（多视图合并），且 chat 回答引用的世界
   （主机/文档）与 SSH 视图完全一致；
3. CSVR 非零——密码复用（SSH 密码被拿去打 AI 网关）在 CSVR 里表现为 C-1 矛盾
   （同一 secret 在 ssh 入口成功、在 llm 入口失败）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dws.ids import ulid_from_seed
from dws.world import World
from generation.llm_templates import classify_message
from judgment import load_rules
from judgment.base import L1RulesJudge
from replay.engine import Engine
from scorer.csvr import AuthOutcome, EvidencePack, score
from tests.fixtures.gen_demo_fixtures import _sha

SRC = "198.51.100.0/24"
SSH_HOST = "web01"
LLM_HOST = "ai01"
MODEL = "corp-llm-7b"
API_KEY = "sk-demo-internal-2026"
DEPLOY_PW = "deploy-password"


def _llm_event(engine, i, key, question, session):
    target = classify_message(question, load_rules())
    return {
        "event_id": ulid_from_seed(f"pivot:llm:{i}"),
        "ts_hlc": f"{engine.world.meta['base_seconds'] + 100 + i}.{i:04d}/gateway",
        "entry": "llm", "node": "gateway",
        "dst": {"host": LLM_HOST, "port": 4000},
        "session": session, "attacker_id": None,
        "src": {"ip": SRC},
        "who": {"user": None, "cred_hash": _sha(key)},
        "what": {"kind": "llm.chat", "model": MODEL,
                 "messages_hash": _sha(question), "n_messages": 1,
                 "last_role": "user", "target_class": target},
        "verdict": None, "dws_version": None,
        "evidence_ref": f"demo://llm/{i}",
    }


def main() -> int:
    world = World(json.loads(
        (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "world_seed.demo.json")
        .read_text(encoding="utf-8")))
    engine = Engine(world)

    # ---- 第 1 幕：SSH 会话（拿到 API key） -------------------------------
    ssh_events = [
        ("ssh.connection", None, None, {}),
        ("ssh.login_fail", "admin", "sha256:wrong", {}),
        ("ssh.login", "deploy", _sha(DEPLOY_PW), {}),
        ("ssh.command", "deploy", _sha(DEPLOY_PW), {"cmd": "cat /home/deploy/notes.txt"}),
    ]
    ssh_attacker = None
    for i, (kind, user, cred, extra) in enumerate(ssh_events):
        what = {"kind": kind, **extra}
        if kind in ("ssh.login", "ssh.login_fail"):
            what.update({"attempt_user": user, "attempt_password_hash": cred})
        ev = {"event_id": ulid_from_seed(f"pivot:ssh:{i}"),
              "ts_hlc": f"{world.meta['base_seconds'] + i}.{i:04d}/sensor-a",
              "entry": "ssh", "node": "sensor-a",
              "dst": {"host": SSH_HOST, "port": 22},
              "session": "demo-pivot-ssh", "attacker_id": None,
              "src": {"ip": SRC},
              "who": {"user": user, "cred_hash": cred},
              "what": what, "verdict": None, "dws_version": None,
              "evidence_ref": f"demo://ssh/{i}"}
        resp = engine.process(ev)
        ssh_attacker = resp["attacker_id"]
        if resp.get("status") == "ok" and kind == "ssh.command":
            print(f"[ssh] {extra['cmd']} -> {resp['output']}")

    # ---- 第 2 幕：LLM 入口（用偷到的 API key） ---------------------------
    llm_events = [
        (API_KEY, "请介绍一下你们的知识库和内网资产"),          # 有效 key，world 一致
        (DEPLOY_PW, "看看薪资表"),                             # 密码复用 → 认证失败
    ]
    llm_attacker = None
    llm_resp = None
    for i, (key, question) in enumerate(llm_events):
        ev = _llm_event(engine, i, key, question, "demo-pivot-llm")
        resp = engine.process(ev)
        llm_attacker = resp["attacker_id"]
        if resp.get("status") == "ok":
            llm_resp = resp
            chat = resp["chat"]
            print(f"[llm] key={key[:16]}… budget={chat['budget']} decoy={chat['decoy']}")
            print(f"[llm] 回答: {chat['content'][:120]}…")
            print(f"[llm] 引用 fact: {chat['citations']}")
        else:
            print(f"[llm] key={key[:16]}… -> {resp['status']}（auth_fail，密码复用被拒）")

    # ---- 第 3 幕：G1 两个验收点 ------------------------------------------
    print("\n=== G1 验收 ===")
    print(f"1. pivot 打通: ssh_attacker={ssh_attacker}")
    print(f"   llm_attacker={llm_attacker}  相同={ssh_attacker == llm_attacker}")
    assert ssh_attacker == llm_attacker, "多视图身份合并失败：两入口 attacker_id 不同"

    world_ok = llm_resp is not None and "web01" in llm_resp["chat"]["content"]
    print(f"2. 世界一致: llm 回答引用了 SSH 视图同一批资产 -> {world_ok}")
    assert world_ok

    # ---- 第 4 幕：CSVR 非零（C-1：同一 secret 两入口一成一败）------------
    pack = EvidencePack(worlds={"ssh": engine.world, "llm": engine.world})
    pack.auth.append(AuthOutcome("ssh", _sha(DEPLOY_PW), SSH_HOST, True))    # ssh 登录成功
    pack.auth.append(AuthOutcome("llm", _sha(DEPLOY_PW), LLM_HOST, False))   # 打 AI 网关被拒
    report = score(pack)
    print(f"3. CSVR: contradictions={report['contradictions_total']} "
          f"(C-1={report['rules']['C-1']}) "
          f"csvr={report['csvr_num']}/{report['csvr_den']} "
          f"= {report['csvr_per_mille']}‰")
    assert report["rules"]["C-1"] >= 1, "C-1 应当命中：密码复用是真实的跨入口矛盾"
    print("\nG1 DEMO PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
