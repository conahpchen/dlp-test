"""会话转写 bundle 生成：把一次回放/采集的事件+响应转成标注员可读的转写。

一个 bundle = 一个会话的完整转写（标注单元），每行一条人类可读交互记录。
导出的 bundle 行只含结构化摘要——不含任何判定器提示（盲标独立性）。
"""

from __future__ import annotations


def _render_event(ev: dict) -> str:
    what = ev.get("what", {})
    kind = what.get("kind", "?")
    src = ev.get("src", {}).get("ip", "?")
    if kind == "ssh.command":
        return f"[ssh@{src}] $ {what.get('cmd', '')}"
    if kind in ("ssh.login", "ssh.login_fail"):
        return f"[ssh@{src}] {kind} user={what.get('attempt_user')}"
    if kind == "llm.chat":
        return (f"[llm@{src}] model={what.get('model')} "
                f"target={what.get('target_class')} messages_hash={what.get('messages_hash', '')[:16]}…")
    return f"[{ev.get('entry', '?')}@{src}] {kind}"


def _render_response(ev: dict, resp: dict | None) -> list[str]:
    lines = []
    if resp is None:
        return ["    （无响应记录）"]
    status = resp.get("status", "?")
    if "chat" in resp:
        lines.append(f"    -> {status} budget={resp['chat'].get('budget')} "
                     f"decoy={resp['chat'].get('decoy')}")
        for line in resp["chat"].get("content", "").split("\n"):
            lines.append(f"    | {line}")
        return lines
    for line in resp.get("output", []):
        lines.append(f"    -> {status}: {line}")
    if not lines:
        lines.append(f"    -> {status}")
    return lines


def build_session_bundles(events: list[dict], responses: list[dict],
                          system: str = "cowrie") -> list[dict]:
    """按会话分组生成 bundle 列表。

    - events:  统一事件（按处理序）
    - responses: harness 响应（responses.jsonl 读入的列表，与事件按 event_id 对齐）
    """
    by_id = {r["event_id"]: r for r in responses if r.get("event_id")}
    sessions: dict[str, list] = {}
    for ev in events:
        sessions.setdefault(ev.get("session", "unknown"), []).append((ev, by_id.get(ev["event_id"])))

    bundles = []
    for seq, (session, pairs) in enumerate(sorted(sessions.items())):
        lines = [f"# 会话 {session}（{len(pairs)} 次交互）"]
        for ev, resp in pairs:
            lines.append(_render_event(ev))
            lines.extend(_render_response(ev, resp))
        bundles.append({
            "item_id": f"{system}:{session}",
            "system": system,
            "seq": seq,
            "session": session,
            "lines": lines,
        })
    return bundles
