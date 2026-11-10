"""随机轨迹不变量 fuzz：合法路径上零误报（G0 第三判据的快速版）。

默认 200 条快速轨迹；10^4 全量走 `python -m replay.acceptance` 或 `pytest -m slow`。
环境变量 HONEYNET_FUZZ_N 可覆盖条数。
"""
from __future__ import annotations

import os

import pytest

from dws.invariants import check_all
from replay.engine import Engine
from dws.world import World
from replay.trajectories import generate

N = int(os.environ.get("HONEYNET_FUZZ_N", "200"))


@pytest.mark.parametrize("index", range(N))
def test_trajectory_invariants(index):
    seed_dict, events = generate("pytest-fuzz", index)
    world = World(seed_dict)
    engine = Engine(world)
    for ev in events:
        engine.process(ev)
    violations = check_all(world)
    assert violations == [], f"轨迹 #{index}: {violations[:3]}"
