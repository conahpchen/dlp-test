"""DWS 实体事实（契约 12 §2）。

追加式版本化：修改事实 = 追加新行（新 fact_id、version+1），旧行永不变更——
可回放、可审计、矛盾可定位到版本。

实体稳定 id = uuid5(类, 业务键)；事实行 id = uuid5(实体 id, 版本)。见 ids.py。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .ids import entity_uuid, fact_uuid

# 实体类 -> 业务键提取（语义冻结：改动即改变全部实体身份）
# credential 键 = (属主, 密钥)：同一属主同一密钥才是同一实体；
# 密码跨属主复用是合法现实（C 类矛盾的真实来源），不合并实体。
# object（v1.3）：开放式业务对象（工单/公告/资产条目…），键 = (type, object_key)；
# 新业务类型经类型注册表（contracts/object_types/*.yaml）声明，零 DDL 新增。
ENTITY_KEYS: dict[str, Callable[[dict], str]] = {
    "host": lambda p: p["hostname"],
    "identity": lambda p: p["username"],
    "credential": lambda p: f"{p['owner_entity_id']}:{p['secret_hash']}",
    "data": lambda p: p["path"],
    "relation": lambda p: f"{p['from_entity_id']}->{p['to_entity_id']}:{p['kind']}",
    "timeline": lambda p: p["event_id_ref"],
    "object": lambda p: f"{p['type']}:{p['object_key']}",
}

# 各实体类 payload 的列（与 dws/sql 迁移一致，契约 §2.1–§2.6、§13.2）
PAYLOAD_COLUMNS: dict[str, tuple[str, ...]] = {
    "host": ("hostname", "ip", "os", "services", "banner"),
    "identity": ("username", "role", "role_bindings", "mfa", "last_login_hlc"),
    "credential": ("secret_hash", "owner_entity_id", "valid_on", "revoked_at_hlc"),
    "data": ("path", "owner_entity_id", "ctime_hlc", "mtime_hlc", "kind", "content", "canary", "deleted"),
    "relation": ("from_entity_id", "to_entity_id", "kind", "ports"),
    "timeline": ("event_id_ref", "object_ref", "kind"),
    "object": ("type", "object_key", "data"),
}


@dataclass
class Fact:
    """一行事实（契约 §2.0 公共列 + 类内 payload）。rows 列表按 version 升序维护。"""

    cls: str
    fact_id: str
    entity_id: str
    version: int
    hlc: str
    node_id: str
    attacker_id: Optional[str]
    world_version: int
    superseded_by: Optional[str]
    payload: dict

    @staticmethod
    def new(cls: str, payload: dict, *, version: int, hlc: str, node_id: str,
            attacker_id: Optional[str], world_version: int) -> "Fact":
        eid = entity_uuid(cls, ENTITY_KEYS[cls](payload))
        return Fact(
            cls=cls,
            fact_id=fact_uuid(eid, version),
            entity_id=eid,
            version=version,
            hlc=hlc,
            node_id=node_id,
            attacker_id=attacker_id,
            world_version=world_version,
            superseded_by=None,
            payload=dict(payload),
        )

    def to_row(self) -> dict:
        """输出形态（world_diff.json 用；canonical 序列化排序键）。"""
        return {
            "cls": self.cls,
            "fact_id": self.fact_id,
            "entity_id": self.entity_id,
            "version": self.version,
            "hlc": self.hlc,
            "node_id": self.node_id,
            "attacker_id": self.attacker_id,
            "world_version": self.world_version,
            "superseded_by": self.superseded_by,
            "payload": dict(self.payload),
        }
