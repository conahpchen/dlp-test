"""最小身份解析器（契约 12 §2.3；13 §02.3）。

M0 最小版：IP(/24) + cred_hash 拼接指纹 -> attacker_id。
多视图（JA3 / 行为 / 时间窗）是 M1 的"身份解析完整版"，接口不变。
"""
from __future__ import annotations

import hashlib


def resolve(event: dict) -> str:
    """由统一事件的 src/who 视图推导稳定 attacker_id（同视图 -> 同 id）。"""
    ip24 = event["src"]["ip"]                       # bridge 已做 /24 截断（合规 T6）
    who = event.get("who") or {}
    cred = who.get("cred_hash") or ""
    user = who.get("user") or ""
    key = f"{ip24}|{cred}|{user}"
    return "atk-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
