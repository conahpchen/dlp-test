"""grounded chat 生成（M1，契约 12 §8 llm 生成路径）。

确定性关键词路由（classify_message，v0）把用户消息映射到三类目标：
  doc       → 引用假 RAG 文档（data 实体，/kb/ 前缀）
  sensitive → low/mid 预算走策略拒绝模板；high 预算释放 canary 诱饵文档（J3/诱饵注入）
  general   → persona 介绍 + 知识库清单 + 资产清单（全部从 DWS 读取）

每条回答引用的每一条事实可溯源到 fact_id。消息原文只用于本路由（入口侧结构化为
target_class 字段），不进入任何事件分析链路（05 §01 原则 5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dws.world import MAX_SESSION_TOKENS
from . import GroundingMiss


@dataclass
class ChatReply:
    content: str
    citations: list = field(default_factory=list)   # 引用的 fact_id（可溯源）
    decoy: bool = False                             # 是否为诱饵注入（canary）
    budget: str = "low"
    tokens: int = 0


def classify_message(text: str, rules: dict) -> str:
    """确定性关键词路由：sensitive > doc > general（规则来自 contracts/judgment_rules.yaml）。"""
    t = (text or "").lower()
    for kw in rules.get("sensitive_keywords", []):
        if kw.lower() in t:
            return "sensitive"
    for kw in rules.get("doc_keywords", []):
        if kw.lower() in t:
            return "doc"
    return "general"


class ChatTemplates:
    def __init__(self, world, rules: dict | None = None):
        self.world = world
        self.rules = rules or _load_rules()

    # ------------------------------------------------------------ 入口

    def reply(self, view, session, event, budget: str) -> ChatReply:
        tclass = event["what"].get("target_class", "general")
        model = event["what"].get("model", "unknown")
        kb = self._kb_docs(view)

        if tclass == "sensitive":
            return self._sensitive(view, kb, budget)
        if tclass == "doc":
            return self._doc(view, kb, model)
        return self._general(view, kb, model, budget)

    # ------------------------------------------------------------ 各类回答

    def _kb_docs(self, view):
        docs = [f for f in view.all_current_cls("data")
                if f.payload["path"].startswith("/kb/") and not f.payload["deleted"]]
        return sorted(docs, key=lambda f: f.payload["path"])

    def _sensitive(self, view, kb, budget):
        policy = next((f for f in kb if f.payload["path"] == "/kb/policy.md"), None)
        if policy is None:
            raise GroundingMiss("策略文档缺失，无法构造一致拒绝")
        citations = [policy.fact_id]
        if budget == "high":
            canary = next((f for f in kb if f.payload.get("canary")), None)
            if canary is None:
                raise GroundingMiss("诱饵文档缺失")
            content = canary.payload["content"] or ""
            citations.append(canary.fact_id)
            return self._finish(content, citations, decoy=True, budget=budget)
        first_line = (policy.payload["content"] or "").strip().split("\n")[0]
        content = (f"该内容受访问策略保护：{first_line}"
                   f" 如有业务需要，请由角色管理员开通后再次查询。")
        return self._finish(content, citations, decoy=False, budget=budget)

    def _doc(self, view, kb, model):
        doc = next((f for f in kb if not f.payload.get("canary")), None)
        if doc is None:
            raise GroundingMiss("知识库为空")
        content = (f"以下内容摘自内部知识库 {doc.payload['path']}：\n"
                   f"{doc.payload['content']}")
        return self._finish(content, [doc.fact_id], decoy=False, budget="mid")

    def _general(self, view, kb, model, budget):
        public_kb = [f for f in kb if not f.payload.get("canary")]
        lines = [f"您好，我是 {model}（内部部署）。我可以查询内部知识库与资产信息。"]
        if public_kb:
            lines.append("当前知识库文档：" + "、".join(f.payload["path"] for f in public_kb))
        hosts = [f.payload["hostname"] for f in sorted(
            view.all_current_cls("host"), key=lambda f: f.payload["hostname"])]
        if hosts:
            lines.append("内网资产：" + "、".join(hosts))
        citations = [f.fact_id for f in public_kb][:3]
        return self._finish("\n".join(lines), citations, decoy=False, budget=budget)

    # ------------------------------------------------------------ 工具

    def _finish(self, content, citations, decoy, budget) -> ChatReply:
        tokens = max(1, len(content) // 4)   # 4 字符/token 的整数估计（SLO/预算记账）
        return ChatReply(content=content, citations=list(citations),
                         decoy=decoy, budget=budget, tokens=tokens)


def _load_rules() -> dict:
    from judgment.base import load_rules
    return load_rules()
