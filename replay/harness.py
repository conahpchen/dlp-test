"""回放 harness CLI（契约 12 §6）。

    python -m replay.harness run    --world seed.json --events events.jsonl --out DIR
                                    [--pg-dsn DSN]     # 内网部署：事件落库 + 世界回填
    python -m replay.harness verify --world seed.json --events events.jsonl [--runs 2]
    python -m replay.harness hash   --out DIR          # 复核已产出目录的清单哈希

verify 跑 N 遍并比对 responses.jsonl / world_diff.json 的 SHA-256 —— G0 第二判据。
metrics.json 含 wall-clock，**不参与**哈希比对（§6.3）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import jsonschema

from dws.world import World
from dws import loader_sql
from .canonical import dumps_canonical, sha256_hex, write_json, write_jsonl
from .engine import Engine

HARNESS_VERSION = "0.1.0"
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "event.schema.v1.json"

_SCHEMA = None


def load_schema():
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _SCHEMA


def validate_events(events) -> None:
    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    for i, ev in enumerate(events):
        errors = sorted(validator.iter_errors(ev), key=lambda e: e.path)
        if errors:
            first = errors[0]
            raise ValueError(f"事件 #{i} 不符合 event.schema.v1: {first.message}")


def load_events(path: Path) -> list[dict]:
    events = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: JSON 解析失败: {e}")
    return events


def world_diff(world: World) -> dict:
    """世界全量差异（契约 §6.2）：追加式事实全记录 + 会话 + 元数据。"""
    facts = {}
    for cls, entities in world.facts.items():
        rows = []
        for eid in sorted(entities):
            rows.extend(f.to_row() for f in entities[eid])
        rows.sort(key=lambda r: (r["world_version"], r["entity_id"], r["version"]))
        facts[cls] = rows
    return {
        "world_version": world.meta["world_version"],
        "meta": {"seed": world.meta["seed"], "node": world.meta["node"],
                 "subnets": world.meta["subnets"], "now_hlc": world.meta["now_hlc"]},
        "sessions": [
            {"session_id": s.session_id, "attacker_id": s.attacker_id,
             "user": s.user, "pinned_version": s.pinned_version,
             "started_hlc": s.started_hlc, "host_entity_id": s.host_entity_id,
             "new_entities": len(s.new_entities), "tokens_used": s.tokens_used}
            for s in world.sessions.values()],
        "facts": facts,
    }


def run_replay(seed: dict, events: list[dict], validate: bool = True):
    """跑一遍回放，返回 (responses, diff, world, metrics)。纯确定性。"""
    if validate:
        validate_events(events)
    world = World(seed)
    engine = Engine(world)
    t0 = time.perf_counter()
    responses = [engine.process(ev) for ev in events]
    wall_ms = int((time.perf_counter() - t0) * 1000)
    metrics = {
        "events": engine.metrics["events"],
        "fallbacks": engine.metrics["fallbacks"],
        "cas_conflicts": world.cas_conflicts,
        "commits_ok": world.commits_ok,
        "commits_rejected": world.commits_rejected,
        "wall_ms_total": wall_ms,
        "wall_ms_p95_event": wall_ms,  # M0 单线程批处理近似；在线路径按事件计时（SLO 12 §7）
    }
    return responses, world_diff(world), world, metrics


def cmd_run(args) -> int:
    seed = json.loads(Path(args.world).read_text(encoding="utf-8"))
    events = load_events(Path(args.events))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    responses, diff, world, metrics = run_replay(seed, events)

    responses_sha = write_jsonl(out / "responses.jsonl", responses)
    diff_sha = write_json(out / "world_diff.json", diff)
    seed_sha = sha256_hex(dumps_canonical(seed))
    events_sha = sha256_hex(dumps_canonical(events))
    manifest = {
        "harness_version": HARNESS_VERSION,
        "world_seed_sha256": seed_sha,
        "events_sha256": events_sha,
        "responses_sha256": responses_sha,
        "world_diff_sha256": diff_sha,
        "events_count": len(events),
        "world_version": diff["world_version"],
    }
    write_json(out / "run_manifest.json", manifest)
    write_json(out / "metrics.json", metrics)   # 不参与哈希比对（§6.3）

    print(f"run: events={len(events)} world_version={diff['world_version']} "
          f"responses_sha256={responses_sha[:16]}… diff_sha256={diff_sha[:16]}…")

    if args.pg_dsn:
        psycopg = loader_sql.require_psycopg()
        with psycopg.connect(args.pg_dsn) as conn:
            for ev in events:
                loader_sql.insert_event(conn, ev)
            loader_sql.apply_world_diff(conn, diff)
        safe_dsn = re.sub(r"(password=)\S*", r"***", args.pg_dsn)
        print(f"pg: 事件已落 event 表，世界差异已回填 DWS（{safe_dsn}）")
    return 0


def _run_once_to(tmpdir: Path, seed, events) -> dict:
    responses, diff, world, metrics = run_replay(seed, events)
    responses_sha = write_jsonl(tmpdir / "responses.jsonl", responses)
    diff_sha = write_json(tmpdir / "world_diff.json", diff)
    return {"responses_sha256": responses_sha, "world_diff_sha256": diff_sha,
            "metrics": metrics}


def cmd_verify(args) -> int:
    seed = json.loads(Path(args.world).read_text(encoding="utf-8"))
    events = load_events(Path(args.events))
    validate_events(events)
    runs = []
    for i in range(args.runs):
        with tempfile.TemporaryDirectory() as td:
            runs.append(_run_once_to(Path(td), seed, events))
    ref = runs[0]
    for i, r in enumerate(runs[1:], 2):
        for key in ("responses_sha256", "world_diff_sha256"):
            if r[key] != ref[key]:
                print(f"VERIFY FAIL: run#{i} {key} 不一致")
                return 1
    print(f"VERIFY PASS: {args.runs} 次回放逐字节一致 "
          f"(responses={ref['responses_sha256'][:16]}… diff={ref['world_diff_sha256'][:16]}…)")
    return 0


def cmd_hash(args) -> int:
    out = Path(args.out)
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    ok = True
    for name, key in (("responses.jsonl", "responses_sha256"),
                      ("world_diff.json", "world_diff_sha256")):
        actual = sha256_hex((out / name).read_bytes())
        if actual != manifest[key]:
            print(f"HASH MISMATCH: {name}")
            ok = False
    print("HASH OK" if ok else "HASH FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="replay.harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="执行一次回放并产出工件")
    p.add_argument("--world", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--pg-dsn", default=None, help="Postgres DSN（可选，内网部署用）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("verify", help="跑 N 遍并校验逐字节一致（G0）")
    p.add_argument("--world", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--runs", type=int, default=2)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("hash", help="复核产出目录清单哈希")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_hash)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
