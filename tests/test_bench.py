"""M2 benchmark 核心：脚本确定性、cowrie 适配器、单系统报告。"""

from pathlib import Path

from experiments.bench.adapters import ADAPTERS, CowrieAdapter
from experiments.bench.script import build_attack_script
from experiments.bench.runner import bench_report, main as runner_main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_attack_script_deterministic_and_protocol_covered():
    s1 = build_attack_script("m2-benchmark", n_sessions=3)
    s2 = build_attack_script("m2-benchmark", n_sessions=3)
    assert s1 == s2
    protos = {a["proto"] for a in s1}
    assert protos == {"ssh", "http"}
    ops = {a["op"] for a in s1 if a["proto"] == "ssh"}
    assert {"connect", "auth_fail", "auth", "exec", "close"} <= ops


def test_cowrie_adapter_parse_and_schema():
    """cowrie 日志样例 → 统一事件 → schema 合法 → 单系统报告可产出。"""
    events = CowrieAdapter().parse_logs(FIXTURES / "cowrie.sample.jsonl")
    assert len(events) == 5
    adapter = ADAPTERS["cowrie"]
    adapter.validate(events)                       # 不抛即 schema 合法
    report = bench_report("cowrie", events)
    assert report["system"] == "cowrie" and report["events_total"] == 5
    assert report["valid"] is False                # recall 区间缺失 → 点值无效（§4.3）


def test_runner_cli_writes_report(tmp_path):
    out = tmp_path / "report.json"
    rc = runner_main(["--system", "cowrie",
                      "--logs", str(FIXTURES / "cowrie.sample.jsonl"),
                      "--out", str(out)])
    assert rc == 0 and out.exists()
    text = out.read_text(encoding="utf-8")
    assert '"system": "cowrie"' in text
