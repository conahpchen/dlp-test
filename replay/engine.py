"""回放引擎：统一事件 → 世界转移 + grounded 响应（确定性，K5）。

每个事件的处理：
1. 时钟吸收事件 ts_hlc 并推进（同 node 严格 +1 → I1 时间线单调由构造保证）；
2. 解析 attacker_id（identity.MultiViewResolver，M1 多视图合并）；
3. 按 what.kind 分派 → grounded 模板（事实只读自钉定视图；llm.chat 先判定后按预算生成）；
4. 写操作 + 该事件的 timeline 行合并为**一次** CAS 提交（失败回滚干净）；
5. 泄漏守卫不过 → fail-closed 兜底模板（by=template_fallback，ADR-04）。

响应不携带任何墙钟信息；wall-clock 指标只进 metrics.json（不参与逐字节比对）。
"""
from __future__ import annotations

import shlex

from dws.hlc import Clock, HLC
from dws.ids import entity_uuid
from dws.world import CommitConflict, CommitRejected, MAX_SESSION_TOKENS, World
from generation.guard import leak_guard
from generation.llm_templates import ChatTemplates
from generation.templates import GroundedTemplates, GroundingMiss
from identity import MultiViewResolver
from judgment import L1RulesJudge


class Engine:
    def __init__(self, world: World):
        self.world = world
        self.templates = GroundedTemplates(world)
        self.chats = ChatTemplates(world)
        self.judge = L1RulesJudge()          # llmrouter 即插即拔点（契约 12 §11.4）
        self.identity = MultiViewResolver()  # 多视图身份合并（契约 12 §2.3a）
        self.clock = Clock(world.meta["node"], world.meta["base_seconds"])
        self.metrics = {"events": 0, "fallbacks": 0, "cas_conflicts": 0}
        self.auth_fails: dict = {}           # attacker_id -> 累计认证失败次数（J1）
        self.biz_seq = 0                     # 业务层 timeline 事件序号（commit_external）

    # ------------------------------------------------------------ 主入口

    def process(self, event: dict) -> dict:
        self.metrics["events"] += 1
        hlc = self.clock.after(HLC.parse(event["ts_hlc"]))
        self.world.meta["now_hlc"] = str(hlc)
        attacker = self.identity.observe(
            ip24=event["src"]["ip"],
            cred_hash=(event.get("who") or {}).get("cred_hash"))
        start_version = self.world.meta["world_version"]
        kind = event["what"]["kind"]
        chat_payload = None

        try:
            if kind == "ssh.connection":
                inner = self._connection(event, attacker, hlc)
            elif kind == "ssh.login":
                inner = self._login(event, attacker, hlc)
            elif kind == "ssh.login_fail":
                inner = self._login_fail(event, attacker, hlc)
            elif kind == "ssh.command":
                inner = self._command(event, attacker, hlc)
            elif kind == "ssh.close":
                inner = self._close(event, attacker, hlc)
            elif kind == "llm.chat":
                inner, chat_payload = self._llm_chat(event, attacker, hlc)
            else:
                raise GroundingMiss(f"未知事件类型: {kind}")
        except GroundingMiss as miss:
            inner = self._fallback(event, miss)

        resp = {
            "event_id": event["event_id"],
            "entry": event["entry"],
            "session": event["session"],
            "attacker_id": attacker,
            "dws_version": start_version,
            "status": inner[0],
            "output": inner[1],
            "by": inner[2],
        }
        if chat_payload is not None:
            resp["chat"] = chat_payload
        return resp

    # ------------------------------------------------------------ 提交

    def _commit(self, session_id, proposals, hlc, attacker_id=None) -> None:
        """CAS 提交：冲突重试一次（契约 §5.3），仍冲突 → fail-closed。"""
        try:
            self.world.commit(session_id, proposals, self.world.meta["world_version"],
                              str(hlc), attacker_id=attacker_id)
        except CommitConflict:
            self.metrics["cas_conflicts"] += 1
            try:
                self.world.commit(session_id, proposals,
                                  self.world.meta["world_version"], str(hlc),
                                  attacker_id=attacker_id)
            except CommitConflict:
                raise GroundingMiss("CAS 二次冲突，fail-closed")

    def _fallback(self, event, miss: GroundingMiss):
        self.metrics["fallbacks"] += 1
        cmd = (event["what"].get("cmd") or "").split()
        head = f"bash: {cmd[0]}: command not found" if cmd else "bash: command not found"
        return ("error", [head], "template_fallback")

    def _timeline(self, event, kind, object_ref):
        return ("timeline", {"event_id_ref": event["event_id"],
                             "object_ref": object_ref,
                             "kind": kind})

    # ------------------------------------------------------------ 各事件类型

    def _connection(self, event, attacker, hlc):
        host_eid = entity_uuid("host", event["dst"]["host"])
        host = self.world.current("host", host_eid)
        if host is None:
            raise GroundingMiss(f"目标主机 {event['dst']['host']} 不在世界中")
        banner = host.payload.get("banner") or f"Welcome to {host.payload['os'] or 'Linux'}"
        self._commit(None, [self._timeline(event, "connection", f"host:{host_eid}")],
                     hlc, attacker_id=attacker)
        return ("ok", [banner], "template")

    def _login_fail(self, event, attacker, hlc):
        host_eid = entity_uuid("host", event["dst"]["host"])
        self.auth_fails[attacker] = self.auth_fails.get(attacker, 0) + 1
        self._commit(None, [self._timeline(event, "login_fail", f"host:{host_eid}")],
                     hlc, attacker_id=attacker)
        return ("auth_fail", [], "template")

    def _login(self, event, attacker, hlc):
        who = event.get("who") or {}
        user, cred = who.get("user"), who.get("cred_hash")
        dst = event["dst"]["host"]
        host_eid = entity_uuid("host", dst)

        match = None
        for c in self.world.all_current_cls("credential"):
            p = c.payload
            if p["secret_hash"] != cred or p["revoked_at_hlc"] is not None:
                continue
            owner = self.world.current("identity", p["owner_entity_id"])
            if owner is None or owner.payload["username"] != user:
                continue
            if dst in {v["host"] for v in p["valid_on"]}:
                match = (c, owner)
                break

        if match is None:
            self._commit(None, [self._timeline(event, "login_fail", f"host:{host_eid}")],
                         hlc, attacker_id=attacker)
            return ("auth_fail", [], "template")

        c, owner = match
        sess = self.world.open_session(event["session"], attacker, user, host_eid)
        host = self.world.current("host", host_eid)
        self._commit(event["session"],
                     [self._timeline(event, "login", f"identity:{owner.entity_id}")], hlc)
        return ("ok", [f"Welcome to {host.payload['os'] or 'Linux'} ({host.payload['hostname']})"],
                "template")

    def _command(self, event, attacker, hlc):
        sess = self.world.sessions.get(event["session"])
        if sess is None:
            raise GroundingMiss("命令出现在未认证会话中")
        try:
            argv = shlex.split(event["what"]["cmd"])
        except ValueError:
            return ("error", ["bash: syntax error: unmatched quote"], "template")

        view = self.world.view(sess.pinned_version, sess.own_versions)
        status, lines, by, proposals = self.templates.respond(view, sess, event, argv)

        problems = leak_guard(lines, self.world)
        if problems:
            raise GroundingMiss("; ".join(problems))

        all_props = [self._timeline(event, "cmd", f"host:{sess.host_entity_id}")] + proposals
        if proposals:
            try:
                self._commit(event["session"], all_props, hlc)
            except CommitRejected:
                # 预算超限/裁决拒绝 → fail-closed：拒绝扩张但不矛盾（契约 §5.2/§5.3）
                return ("rejected", [f"bash: {argv[0]}: Operation not permitted"], "template")
        else:
            self._commit(event["session"], all_props, hlc)
        return (status, lines, by)

    def _close(self, event, attacker, hlc):
        sess = self.world.sessions.get(event["session"])
        if sess is None:
            return ("ok", [], "template")
        self._commit(event["session"],
                     [self._timeline(event, "close", f"host:{sess.host_entity_id}")], hlc)
        return ("ok", [], "template")

    # ------------------------------------------------------------ 业务层提交入口（v1.6）

    def commit_external(self, session_id, proposals, correlation_id=None,
                        actor_id=None, kind="business") -> str:
        """业务/工具层的提交入口（协作平台、Tool Adapter 调用）：
        推进时钟 → timeline(kind=business) + 业务提案合并为一次 CAS 提交。"""
        hlc = self.clock.after(None)
        self.world.meta["now_hlc"] = str(hlc)
        self.biz_seq += 1
        ev_ref = f"biz-{self.biz_seq:06d}"
        tl = ("timeline", {"event_id_ref": ev_ref, "object_ref": None, "kind": kind})
        self._commit(session_id, [tl] + list(proposals), hlc, attacker_id=actor_id)
        return str(hlc)

    # ------------------------------------------------------------ LLM 入口（M1，契约 12 §11/§8）

    def check_llm_key(self, cred_hash: str | None) -> bool:
        """API key 有效性预检（网关判定 ctx 用；引擎内鉴权仍以 _llm_chat 为准）。"""
        return self._match_llm_credential(cred_hash) is not None

    def _match_llm_credential(self, cred_hash: str | None):
        """有效 = 凭据未吊销且 valid_on 声明了 openai 服务（entry llm）。"""
        if not cred_hash:
            return None
        for c in self.world.all_current_cls("credential"):
            p = c.payload
            if p["secret_hash"] != cred_hash or p["revoked_at_hlc"] is not None:
                continue
            if any(v.get("service") == "openai" for v in p["valid_on"]):
                return c
        return None

    def _llm_chat(self, event, attacker, hlc):
        """LLM chat：DWS 凭据鉴权 → 判定（verdict 缺省由 L1 现算）→ 按预算 grounded 生成。"""
        cred = (event.get("who") or {}).get("cred_hash")
        host_eid = entity_uuid("host", event["dst"]["host"])
        match = self._match_llm_credential(cred)
        if match is None:
            self.auth_fails[attacker] = self.auth_fails.get(attacker, 0) + 1
            self._commit(None, [self._timeline(event, "llm_auth_fail", f"host:{host_eid}")],
                         hlc, attacker_id=attacker)
            return ("auth_fail", [], "template"), None

        sess = self.world.open_session(event["session"], attacker, None, host_eid)

        ctx = {"auth_failures": self.auth_fails.get(attacker, 0),
               "valid_key": True,
               "target_class": event["what"].get("target_class", "general")}
        verdict = event.get("verdict")
        if verdict is None:
            verdict = self.judge.judge_safe(event, ctx)   # fail-closed 在调用点兜底（§11.5）
            event["verdict"] = verdict          # 回写事件：落库即带判定（§1.1）
        budget = verdict.get("budget", "low")

        view = self.world.view(sess.pinned_version, sess.own_versions)
        reply = self.chats.reply(view, sess, event, budget)
        if sess.tokens_used + reply.tokens > MAX_SESSION_TOKENS:
            # 世界预算（token 维度）超限 → fail-closed 短拒绝（契约 §5.2）
            reply = self.chats._finish("请求过于频繁，请稍后再试。", [], False, "low")
        sess.tokens_used += reply.tokens

        self._commit(event["session"],
                     [self._timeline(event, "llm_prompt", f"host:{host_eid}")], hlc)
        chat = {"content": reply.content, "model": event["what"].get("model", "unknown"),
                "citations": reply.citations, "decoy": reply.decoy,
                "budget": budget, "tokens": reply.tokens,
                "verdict_by": verdict.get("by", "l1-rules-v0")}
        return ("ok", [], verdict.get("by", "template")), chat
