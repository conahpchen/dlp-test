"""G0 验收工具（11 §04 M0 验收）。

    python -m replay.acceptance                      # 全量 10^4 轨迹
    python -m replay.acceptance --trajectories 200   # 快速冒烟

产出两项结论：
1. N 条随机轨迹上四条不变量（I1–I4）0 违反 —— G0 第三判据；
2. 前 K 条轨迹回放两次 SHA-256 一致 —— G0 第二判据的抽样复核。
"""
from __future__ import annotations

import argparse
import sys
import time

from dws.invariants import check_all
from dws.world import World
from .canonical import dumps_canonical, sha256_hex
from .engine import Engine
from .harness import world_diff
from .trajectories import generate


def replay_digest(seed_dict, events) -> str:
    world = World(seed_dict)
    engine = Engine(world)
    responses = [engine.process(e) for e in events]
    diff = world_diff(world)
    return sha256_hex(dumps_canonical(responses) + b"\x00" + dumps_canonical(diff))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="replay.acceptance")
    parser.add_argument("--trajectories", type=int, default=10_000)
    parser.add_argument("--seed", default="m0-g0")
    parser.add_argument("--determinism-sample", type=int, default=20)
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    total_events = 0
    violations_total = 0
    bad = []

    for i in range(args.trajectories):
        seed_dict, events = generate(args.seed, i)
        world = World(seed_dict)
        engine = Engine(world)
        for ev in events:
            engine.process(ev)
        total_events += len(events)
        viols = check_all(world)
        if viols:
            violations_total += len(viols)
            bad.append((i, [str(v) for v in viols[:3]]))
        if (i + 1) % 500 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  … {i + 1}/{args.trajectories} 轨迹 "
                  f"({total_events} 事件, {elapsed:.1f}s)", flush=True)

    det_fail = []
    for i in range(min(args.determinism_sample, args.trajectories)):
        seed_dict, events = generate(args.seed, i)
        if replay_digest(seed_dict, events) != replay_digest(seed_dict, events):
            det_fail.append(i)

    wall = time.perf_counter() - t0
    print(f"acceptance: trajectories={args.trajectories} events={total_events} "
          f"violations={violations_total} determinism_failures={len(det_fail)} "
          f"wall={wall:.1f}s")
    for i, vs in bad[:5]:
        print(f"  轨迹 #{i}: {vs}")
    if bad or det_fail:
        print("ACCEPTANCE FAIL")
        return 1
    print("ACCEPTANCE PASS (G0: 不变量 0 违反 + 回放一致)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
