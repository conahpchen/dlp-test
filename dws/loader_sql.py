"""Postgres 装载（契约 12 §2；deploy/docker-compose.yml 起库后使用）。

不写 ORM（11 §09）：全部裸 SQL。psycopg 延迟导入——Windows 开发机没有
Postgres/Docker 时，其余模块与测试不受影响（集成测试标记 skip）。

用法（内网 Linux 部署后）:
    export HONEYNET_PG_DSN="postgresql://honeynet:***@127.0.0.1:5432/honeynet"
    python -m replay.harness run ... --pg-dsn "$HONEYNET_PG_DSN"   # 事件落库 + 世界回填
"""
from __future__ import annotations

import json

try:
    import psycopg
except ImportError:  # 本机无 Postgres 依赖时保持可导入
    psycopg = None

from .facts import PAYLOAD_COLUMNS

# 事件表冗余列
_EVENT_KEYS = ("ts_hlc", "entry", "node", "session", "attacker_id")


def require_psycopg():
    if psycopg is None:
        raise RuntimeError("psycopg 未安装: pip install 'psycopg[binary]'（内网部署机执行）")
    return psycopg


def insert_event(conn, event: dict) -> None:
    """统一事件落 event 表（契约 §2.10）。land_seq 由库内 bigserial 生成。

    v1.3 起事件可带 world_id / correlation_id；002 迁移未执行时自动回退基础列。"""
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
    cols = {k: event.get(k) for k in _EVENT_KEYS}
    base = (event["event_id"], cols["ts_hlc"], cols["entry"], cols["node"],
            cols["session"], cols["attacker_id"], payload)
    with conn.cursor() as cur:
        try:
            cur.execute(
                """INSERT INTO event (event_id, ts_hlc, entry, node, session, attacker_id, payload,
                                      world_id, correlation_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_id) DO NOTHING""",
                base + (event.get("world_id"), event.get("correlation_id")))
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(
                """INSERT INTO event (event_id, ts_hlc, entry, node, session, attacker_id, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (event_id) DO NOTHING""",
                base[:7])


def apply_world_diff(conn, diff: dict) -> None:
    """把回放产出的 world_diff 落库：追加事实行 + 推进 dws_meta。单事务。"""
    with conn.cursor() as cur:
        for cls, rows in diff["facts"].items():
            cols = PAYLOAD_COLUMNS[cls]
            common = ("fact_id", "entity_id", "version", "hlc", "node_id",
                      "attacker_id", "world_version", "superseded_by")
            sql = (
                f"INSERT INTO {cls} ({', '.join(common + cols)}) "
                f"VALUES ({', '.join(['%s'] * (len(common) + len(cols)))}) "
                f"ON CONFLICT (fact_id) DO NOTHING"
            )
            for row in rows:
                params = [row[k] for k in common]
                params += [json.dumps(row["payload"][c], ensure_ascii=False)
                           if isinstance(row["payload"].get(c), (dict, list))
                           else row["payload"].get(c) for c in cols]
                cur.execute(sql, params)
        cur.execute(
            """INSERT INTO dws_meta (key, value) VALUES ('world_version', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (json.dumps(diff["world_version"]),))
        cur.execute(
            """INSERT INTO dws_meta (key, value) VALUES ('now_hlc', %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (json.dumps(diff["meta"]["now_hlc"]),))
    conn.commit()


def load_seed(conn) -> dict:
    """从当前有效行视图重建世界种子（供在库上继续回放/演示）。"""
    seed = {"meta": {"seed": "from-db", "node": "replay",
                     "subnets": [], "initial_version": 1},
            "initial_facts": {}}
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM dws_meta WHERE key = 'subnets'")
        row = cur.fetchone()
        if row:
            seed["meta"]["subnets"] = row[0]
        for cls in PAYLOAD_COLUMNS:
            cur.execute(f"SELECT * FROM {cls}_current")
            cols = [d[0] for d in cur.description]
            payloads = []
            for rec in cur.fetchall():
                d = dict(zip(cols, rec))
                payloads.append({c: d[c] for c in PAYLOAD_COLUMNS[cls]})
            seed["initial_facts"][cls] = payloads
    return seed
