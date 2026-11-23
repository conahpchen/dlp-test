"""Approved DWS publications for synthetic documentation and catalog graphs."""
from __future__ import annotations

import json
import secrets
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from business.atomic_state import read
from business.publications import queue
from business.storage import location
from dws.ids import entity_uuid
from dws.world import CommitRejected
from generation.guard import leak_guard


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,27}$")
    kind: Literal["document", "database"]
    content: str | None = Field(default=None, min_length=1, max_length=2048)


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^[a-z][a-z0-9_]{0,27}$")
    target: str = Field(pattern=r"^[a-z][a-z0-9_]{0,27}$")


class GraphProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_version: int = Field(ge=1, le=2**53 - 1)
    scope: Literal["public", "branch"]
    nodes: list[GraphNode] = Field(min_length=2, max_length=8)
    edges: list[GraphEdge] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_topology(self):
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("节点编号必须唯一")
        if any((n.content is not None) != (n.kind == "document") for n in nodes.values()):
            raise ValueError("只允许知识文档提供正文")
        edges = {(edge.source, edge.target) for edge in self.edges}
        if len(edges) != len(self.edges):
            raise ValueError("关系必须唯一")
        touched = set()
        degree = {}
        for source, target in edges:
            if (source not in nodes or target not in nodes or nodes[source].kind != "document"
                    or nodes[target].kind != "database"):
                raise ValueError("关系必须从知识文档指向同一提案中的数据库目录")
            touched.update((source, target))
            degree[source] = degree.get(source, 0) + 1
            if degree[source] > 2:
                raise ValueError("每个文档最多关联两个数据库")
            degree[target] = degree.get(target, 0) + 1
            if degree[target] > 4:
                raise ValueError("每个数据库最多关联四个文档")
        if touched != nodes.keys():
            raise ValueError("每个节点必须至少参与一条关系")
        return self


def _init(db):
    db.execute("""CREATE TABLE IF NOT EXISTS graph_proposals (
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
        result_version INTEGER, error TEXT)""")


def _row(db, identifier):
    row = db.execute("SELECT * FROM graph_proposals WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise KeyError(identifier)
    return {**dict(row), "payload": json.loads(row["payload"])}


def submit(proposal):
    with queue() as db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT count(*) FROM graph_proposals").fetchone()[0] >= 500:
            raise ValueError("图发布队列已满")
        identifier = secrets.token_hex(16)
        db.execute("INSERT INTO graph_proposals(id,payload,status) VALUES (?,?,'draft')",
                   (identifier, proposal.model_dump_json()))
        return _row(db, identifier)


def list_proposals():
    with queue() as db:
        _init(db)
        ids = db.execute("SELECT id FROM graph_proposals ORDER BY rowid DESC LIMIT 100").fetchall()
        return [_row(db, identifier) for identifier, in ids]


def transition(identifier, action):
    initial, pending, completed = {
        "approve": ("draft", "approved", "active"),
        "withdraw": ("active", "withdraw_pending", "withdrawn"),
    }[action]
    with queue() as db:
        _init(db)
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, identifier)
        if row["status"] in (pending, completed):
            return row
        if row["status"] != initial:
            raise ValueError("当前状态不支持此操作")
        db.execute("UPDATE graph_proposals SET status=? WHERE id=?", (pending, identifier))
        return _row(db, identifier)


def _publish(engine, row, checkpoint):
    world = engine.world
    identifier = row["id"]
    withdrawal = row["status"] == "withdraw_pending"
    operation = "withdraw" if withdrawal else "publish"
    releases = world.meta.get("graph_publications", {})
    previous = releases.get(identifier, {})
    if operation in previous:
        return previous[operation]

    spec = GraphProposal.model_validate(row["payload"])
    branch = spec.scope == "branch"
    origin = spec.base_version
    current = world.meta["world_version"]
    if origin > current:
        raise ValueError("基线必须是已有版本")
    actor = "graph:" + identifier
    branch_session = world.sessions.get(actor)
    own = branch_session.own_versions if branch_session else ()
    base_view = world.view(origin)
    live_view = world.view(origin if branch else current, own)
    now = world.meta["now_hlc"]
    owner = entity_uuid("identity", "root")
    host = live_view.current("host", entity_uuid("host", "db01"))
    service = next((s for s in host.payload["services"] if s["proto"] == "mysql"), None) if host else None
    if not withdrawal and (not service or not base_view.current("identity", owner)):
        raise ValueError("缺少数据库投影或知识文档所有者")
    facts, nodes = [], []
    for node in spec.nodes:
        if node.kind == "document":
            path = f"/kb/graph-{node.id}.md"
            cls, eid = "data", entity_uuid("data", path)
            if not withdrawal and leak_guard(node.content.splitlines(), world):
                raise ValueError("文档超出合成内容边界")
            payload = dict(path=path, kind="file", content=node.content, owner_entity_id=owner,
                           canary=False, deleted=False, ctime_hlc=now, mtime_hlc=now)
            label = path
        else:
            name = "graph_" + node.id
            cls, eid = "object", entity_uuid("object", f"database:{name}")
            payload = dict(type="database", object_key=name, data=dict(
                name=name, host="db01", engine=service["ver"] if service else "MySQL",
                owner="dba01", application="APP-COLLAB", tables=[],
            ))
            label = name

        existing = live_view.current(cls, eid)
        if withdrawal:
            if not existing or existing.world_version != previous.get("publish"):
                raise ValueError("投影节点发生变化，撤回需要重新审核")
            payload = (dict(existing.payload, deleted=True, content="", mtime_hlc=now)
                       if cls == "data" else
                       dict(existing.payload, data=dict(existing.payload["data"], graph_retired=True)))
        elif existing:
            raise ValueError("图节点已存在，请使用新的编号")
        facts.append((cls, payload))
        nodes.append(dict(id=node.id, kind=node.kind, label=label, entity_id=eid, cls=cls))

    facts.append(("object", dict(type="publication_graph", object_key=identifier, data=dict(
        nodes=nodes, edges=[edge.model_dump() for edge in spec.edges], retired=withdrawal,
    ))))
    if branch and branch_session is None:
        world.open_session(actor, actor, None, None, pinned_version=origin)
    try:
        engine.commit_external(actor if branch else None, facts, actor_id=actor,
                               kind="graph_publication", private=branch)
    except CommitRejected:
        if branch and branch_session is None:
            world.sessions.pop(actor, None)
        raise
    version = world.meta["world_version"]
    releases[identifier] = dict(previous, scope=spec.scope, base=origin, **{operation: version})
    world.meta["graph_publications"] = releases
    checkpoint(engine)
    return version


def apply_next(engine, checkpoint):
    from business import publication_retry as retry

    with queue() as db:
        _init(db)
        retry.init(db)
        found = db.execute("SELECT p.id FROM graph_proposals p LEFT JOIN publication_retry r "
                           "ON r.lane='graph' AND r.job=p.id "
                           "WHERE p.status IN ('approved','withdraw_pending') "
                           "AND COALESCE(r.retry_at,0)<=unixepoch() ORDER BY p.rowid LIMIT 1").fetchone()
        row = _row(db, found[0]) if found else None
    if row is None:
        return
    try:
        version = _publish(engine, row, checkpoint)
    except (ValueError, CommitRejected) as exc:
        status, version, error = "rejected", None, str(exc)
    except Exception as exc:
        with queue() as db:
            if not retry.failure(db, "graph", row["id"]):
                return
        status, version, error = "failed", None, type(exc).__name__
    else:
        status = "withdrawn" if row["status"] == "withdraw_pending" else "active"
        error = None
    with queue() as db:
        db.execute("UPDATE graph_proposals SET status=?,result_version=?,error=? WHERE id=? AND status=?",
                   (status, version, error, row["id"], row["status"]))
        retry.clear(db, "graph", row["id"])


def load_branch(publication, plan):
    path = location()
    if not path or not path.is_file():
        raise ValueError("DWS graph checkpoint is unavailable")
    state = read(path)
    release = state["meta"].get("graph_publications", {}).get(publication)
    if (not release or release["scope"] != "branch" or "withdraw" in release or
            release["base"] != plan["world_version"] or state["meta"]["world_id"] != plan["world_id"]):
        raise ValueError("分支不存在、已撤回或与场景基线不一致")
    return release["publish"]


def list_graphs(world, version, branch_version=None):
    view = world.view(version, (branch_version,) if branch_version is not None else ())
    graphs = []
    for fact in view.all_current_cls("object"):
        payload = fact.payload
        if payload["type"] != "publication_graph" or payload["data"]["retired"]:
            continue
        data = payload["data"]
        nodes = []
        for node in data["nodes"]:
            item = view.current(node["cls"], node["entity_id"])
            if not item:
                continue
            projection = {key: node[key] for key in ("id", "kind", "label")}
            if node["kind"] == "document":
                projection["content"] = item.payload["content"]
            else:
                projection["engine"] = item.payload["data"]["engine"]
            nodes.append(projection)
        graphs.append(dict(nodes=nodes, edges=data["edges"], version=fact.world_version))
    return graphs
