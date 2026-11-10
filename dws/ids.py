"""确定性 ID 推导（契约 12 §1.1 event_id=ULID；§2.0 fact_id/entity_id=UUID）。

所有 ID 从种子/内容确定性推导——回放两次产生相同 ID，是"逐字节一致"（契约 §6.3）的前提。
采集侧（Cowrie 桥接）允许真随机 ULID（ulid_now），回放引擎内禁止调用。
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid

# ULID 字母表（Crockford Base32，无 I/L/O/U）
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32_26(n: int) -> str:
    """130 bit 整数 -> 26 个 Crockford 字符（大端定宽）。"""
    if n < 0 or n >= (1 << 130):
        raise ValueError("value out of 130-bit range")
    return "".join(CROCKFORD[(n >> (5 * (25 - i))) & 0x1F] for i in range(26))


def ulid_from_digest(digest: bytes) -> str:
    """从摘要确定性推导 ULID 形态 id（回放/测试用）。"""
    n = int.from_bytes(digest[:17], "big") % (1 << 130)
    return _b32_26(n)


def ulid_from_seed(seed: str) -> str:
    return ulid_from_digest(hashlib.sha256(seed.encode("utf-8")).digest())


def ulid_now() -> str:
    """真 ULID：48bit 毫秒时间戳 + 80bit 随机。仅限采集侧（entries/cowrie/bridge）。"""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = int.from_bytes(os.urandom(10), "big")
    return _b32_26((ts_ms << 80) | rnd)


# 项目固定命名空间（语义冻结的一部分：改动即改变全部实体/事实身份）
_NS = uuid.UUID("5c1f7a52-9b3e-5c1a-9d0e-6a2b8c4d1e01")


def entity_uuid(cls: str, key: str) -> str:
    """实体稳定 id = uuid5(类, 业务键)。业务键见 facts.ENTITY_KEYS（语义冻结）。"""
    return str(uuid.uuid5(_NS, f"{cls}:{key}"))


def fact_uuid(entity_id: str, version: int) -> str:
    """事实行 id = uuid5(实体 id, 版本)。追加式版本化下可确定性重放。"""
    return str(uuid.uuid5(_NS, f"fact:{entity_id}:{version}"))
