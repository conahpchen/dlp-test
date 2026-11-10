"""工位机运行时：任务状态机（7 状态）+ 作息窗口（契约 14 §07.1a/§7.2）。

状态机：IDLE → CHECKING → WORKING → (REPORTING | BLOCKED | COLLABORATING) → …
迁移必须合法（TRANSITIONS），行为必须有任务上下文（state_tasks）——禁止无上下文随机脚本。
确定性：同一 persona + seed → 同一状态迁移与工具调用序列（K5）。
"""

from __future__ import annotations

import random

STATES = ("IDLE", "CHECKING", "WORKING", "BLOCKED", "COLLABORATING",
          "REPORTING", "OFFLINE")

TRANSITIONS = {
    "IDLE": ("CHECKING", "OFFLINE"),
    "CHECKING": ("WORKING", "IDLE"),
    "WORKING": ("REPORTING", "BLOCKED", "COLLABORATING", "IDLE", "OFFLINE"),
    "BLOCKED": ("WORKING", "REPORTING", "IDLE", "OFFLINE"),
    "COLLABORATING": ("WORKING", "REPORTING", "IDLE", "OFFLINE"),
    "REPORTING": ("IDLE", "CHECKING", "WORKING", "OFFLINE"),
    "OFFLINE": ("IDLE", "CHECKING"),
}   # 任何在班状态都可 OFFLINE（下班直接走人，14 号 §07.1a）


def to_minute(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def in_window(windows: list, minute: int) -> bool:
    return any(start <= minute < end for start, end in windows)


class WorkstationAgent:
    """一个角色的工位机 Agent：状态机 + 作息 + Tool Adapter 消费者。"""

    def __init__(self, persona: dict, adapter, seed: str = "0"):
        self.persona = persona
        self.adapter = adapter
        self.agent_id = persona["agent_id"]
        self.state = "OFFLINE"
        self.rng = random.Random(f"{self.agent_id}:{seed}")
        self.tick = 0
        self.windows = [(to_minute(a), to_minute(b))
                        for a, b in persona["schedule"]["windows"]]
        self.transition_log: list = []   # (tick, from, to)

    # ------------------------------------------------------------ 状态机

    def transition(self, new: str) -> dict:
        if new not in TRANSITIONS[self.state]:
            raise ValueError(f"非法状态迁移 {self.state} -> {new}")
        self.transition_log.append({"tick": self.tick, "from": self.state, "to": new})
        self.state = new
        return self._state_event(new)

    def _state_event(self, new: str) -> dict:
        self.tick += 1
        return {
            "event_id": __import__("dws.ids", fromlist=["ulid_from_seed"])
                        .ulid_from_seed(f"{self.agent_id}:state:{self.tick}"),
            "ts_hlc": f"{1792000000 + self.tick:04d}.0000/ws",
            "entry": "agent", "node": "ws",
            "dst": {"host": "oa01", "port": 443},
            "session": f"ws-{self.agent_id}", "attacker_id": None,
            "src": {"ip": self.persona["workstation_ip"]},
            "who": {"user": self.persona["username"], "cred_hash": None},
            "what": {"kind": "agent.state", "state": new},
            "verdict": None, "dws_version": None,
            "evidence_ref": f"ws://{self.agent_id}/state/{self.tick}",
            "world_id": None, "correlation_id": None,
        }

    # ------------------------------------------------------------ 作息

    def in_window(self, minute: int) -> bool:
        return in_window(self.windows, minute)

    # ------------------------------------------------------------ 单步执行

    def step(self, minute: int) -> list[dict]:
        """一个 tick：一次状态迁移 + 至多一次工具调用；返回产生的统一事件。"""
        self.tick += 1
        events: list = []

        if not self.in_window(minute):
            if self.state != "OFFLINE":
                events.append(self.transition("OFFLINE"))
            return events
        if self.state == "OFFLINE":
            events.append(self.transition("IDLE"))

        if self.state == "IDLE":
            events.append(self.transition("CHECKING"))
            return events
        if self.state == "CHECKING":
            r = self.adapter.call("ticket_list",
                                  correlation_id=None)
            events.extend(self.adapter.events[-1:])
            if r["status"] == 200 and r["body"]:
                events.append(self.transition("WORKING"))
            else:
                events.append(self.transition("IDLE"))
            return events
        if self.state == "WORKING":
            tool = self._pick_work_tool()
            if tool:
                try:
                    r = self.adapter.call(tool, correlation_id=None)
                    events.extend(self.adapter.events[-1:])
                    if r["status"] == 200:
                        events.append(self.transition("REPORTING"))
                        return events
                except Exception:  # noqa: BLE001 —— 工具异常 → BLOCKED
                    pass
            events.append(self.transition("BLOCKED"))
            return events
        if self.state == "BLOCKED":
            events.append(self.transition("WORKING"))   # MVP：下一 tick 重试
            return events
        if self.state == "COLLABORATING":
            events.append(self.transition("WORKING"))
            return events
        if self.state == "REPORTING":
            tool = self._pick_report_tool()
            if tool:
                r = self.adapter.call(tool, correlation_id=None,
                                      body={"text": f"[{self.persona['display_name']}] 例行汇报"})
                events.extend(self.adapter.events[-1:])
            events.append(self.transition("IDLE"))
            return events
        return events

    # ------------------------------------------------------------ 任务选择

    def _pick_work_tool(self) -> str | None:
        for t in self.persona.get("state_tasks", {}).get("WORKING", []):
            if t in self.adapter.allowlist:
                return t
        return None

    def _pick_report_tool(self) -> str | None:
        for t in self.persona.get("state_tasks", {}).get("REPORTING", []):
            if t in self.adapter.allowlist:
                return t
        return None
