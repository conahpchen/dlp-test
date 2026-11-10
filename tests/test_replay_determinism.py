"""回放逐字节一致（G0 第二判据；契约 12 §6.3）。"""
from __future__ import annotations

from replay.engine import Engine
from dws.world import World
from replay.canonical import dumps_canonical, sha256_hex
from replay.harness import run_replay


def test_demo_replay_byte_identical(demo_seed, demo_events):
    r1, d1, _, _ = run_replay(demo_seed, demo_events)
    r2, d2, _, _ = run_replay(demo_seed, demo_events)
    assert sha256_hex(dumps_canonical(r1)) == sha256_hex(dumps_canonical(r2))
    assert sha256_hex(dumps_canonical(d1)) == sha256_hex(dumps_canonical(d2))


def test_engine_direct_run_matches_harness(demo_seed, demo_events):
    """引擎直跑与 harness 路径产出一致的规范字节。"""
    world = World(demo_seed)
    engine = Engine(world)
    direct = [engine.process(e) for e in demo_events]
    via_harness, _, _, _ = run_replay(demo_seed, demo_events)
    assert sha256_hex(dumps_canonical(direct)) == sha256_hex(dumps_canonical(via_harness))
