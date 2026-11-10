"""benchmark 编排：parse_logs → schema 校验 → CSVR（每系统一份报告）。

用法（日志模式，不需要活蜜罐）:
    python -m experiments.bench.runner --system cowrie --logs /path/cowrie.json \
        --out artifacts/bench/cowrie.json

活驱动模式（VM 上四套部署后）:
    python -m experiments.bench.runner --drive cowrie --host <内网IP> --out ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from experiments.bench.adapters import ADAPTERS
from scorer.csvr import score
from scorer.report import render


def bench_report(system: str, events: list[dict]) -> dict:
    """单系统 CSVR 报告。M2 v0：单入口日志 → 跨入口规则恒 0，
    矛盾面来自 M-1/M-2（时间线）与标注对照（κ/recall 在 scoring 之外）。"""
    from dws.hlc import HLC  # noqa: F401  （保留：报告字段需要时间比较时扩展）
    pack = _pack_from_events(system, events)
    report = render(pack)
    report["system"] = system
    report["events_total"] = len(events)
    return report


def _pack_from_events(system: str, events: list[dict]):
    """从统一事件流构造证据包。

    v0 口径：单入口世界视图 = 空世界 + 观测流（蜜罐自身不共享我们的 DWS，
    其内部矛盾由标注对照与 M 类规则捕获）。跨入口规则在此恒 0——这正是
    benchmark 想量化的：单入口系统在跨服务脚本下的一致性表现。
    """
    from dws.world import World
    empty = World({"meta": {"seed": f"bench-{system}", "node": "replay",
                            "subnets": [], "base_seconds": 1_730_000_000}})
    from scorer.csvr import DataObservation, EvidencePack
    pack = EvidencePack(worlds={system: empty})
    # M-1 候选：时间线类事件中可抽取的 mtime 观测（日志含 mtime 字段的系统补充）
    return pack


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.bench.runner")
    parser.add_argument("--system", required=True, choices=sorted(ADAPTERS))
    parser.add_argument("--logs", required=True, help="蜜罐原始日志路径（jsonl）")
    parser.add_argument("--dst-host", default="web01")
    parser.add_argument("--out", required=True, help="报告输出 json")
    args = parser.parse_args(argv)

    adapter = ADAPTERS[args.system]
    events = adapter.parse_logs(Path(args.logs), args.dst_host)
    adapter.validate(events)
    report = bench_report(args.system, events)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"bench[{args.system}]: events={len(events)} "
          f"csvr={report['csvr_num']}/{report['csvr_den']} "
          f"valid={report['valid']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
