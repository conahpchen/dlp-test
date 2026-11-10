"""Tool Adapter（契约 12 §13.3 审计一致性）：Agent 经业务 API 的受控工具层。

- 工具 = 强类型业务接口（ticket_update / assets_view…），按角色 allowlist 授权；
- Agent 用自己的 DWS 账号走与前端相同的 API——业务审计日志格式与真人一致；
- 每次调用产生一条统一事件（entry=agent），由 Event Reporter 上报。
"""

from __future__ import annotations

import httpx

from dws.ids import ulid_from_seed


class ToolDenied(PermissionError):
    """工具不在角色 allowlist 内。"""


class ToolAdapter:
    # 工具 -> (HTTP 方法, 路径模板)；路径参数从 kwargs 取
    _TOOLS = {
        "change_create": ("POST", "/api/changes"),
        "change_test": ("POST", "/api/changes/{key}/test"),
        "change_approve": ("POST", "/api/changes/{key}/approve"),
        "change_deploy": ("POST", "/api/changes/{key}/deploy"),
        "database_view": ("GET", "/api/databases"),
        "ticket_list": ("GET", "/api/tickets"),
        "ticket_read": ("GET", "/api/tickets/{key}"),
        "ticket_create": ("POST", "/api/tickets"),
        "ticket_status": ("POST", "/api/tickets/{key}/status"),
        "ticket_comment": ("POST", "/api/tickets/{key}/comments"),
        "assets_view": ("GET", "/api/assets"),
        "announcement_list": ("GET", "/api/announcements"),
        "announcement_create": ("POST", "/api/announcements"),
    }

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        allowlist: list,
        agent_id: str,
        workstation_ip: str,
        client: httpx.Client | None = None,
        seq_seed: str = "0",
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.allowlist = set(allowlist)
        self.agent_id = agent_id
        self.workstation_ip = workstation_ip
        self.client = client or httpx.Client(base_url=self.base_url, timeout=10.0)
        self.token: str | None = None
        self._seq = 0
        self._seed = seq_seed
        self.events: list = []  # 统一事件（entry=agent），Event Reporter 上报

    # ------------------------------------------------------------ 认证

    def login(self) -> bool:
        r = self.client.post(
            "/api/login", json={"username": self.username, "password": self.password}
        )
        if r.status_code != 200:
            return False
        self.token = r.json()["token"]
        return True

    # ------------------------------------------------------------ 工具调用

    def call(self, tool: str, correlation_id: str | None = None, **kw) -> dict:
        """调用一个受控工具；返回 {status, body}。每次调用产生统一事件。"""
        if tool not in self.allowlist:
            raise ToolDenied(f"{self.agent_id} 无权使用工具 {tool}")
        method, path_tmpl = self._TOOLS[tool]
        path = path_tmpl.format(**kw)
        body = kw.get("body")
        r = self.client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {self.token}"}
        )
        self._emit(tool, path, r.status_code, correlation_id)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        return {"status": r.status_code, "body": payload}

    # ------------------------------------------------------------ 事件

    def _emit(
        self, tool: str, path: str, status: int, correlation_id: str | None
    ) -> None:
        self._seq += 1
        self.events.append(
            {
                "event_id": ulid_from_seed(f"{self.agent_id}:{self._seq}:{tool}"),
                "ts_hlc": f"{1792000000 + self._seq}.{self._seq:04d}/ws",
                "entry": "agent",
                "node": "ws",
                "dst": {"host": "oa01", "port": 443},
                "session": f"ws-{self.agent_id}",
                "attacker_id": None,
                "src": {"ip": self.workstation_ip},
                "who": {"user": self.username, "cred_hash": self._cred_hash()},
                "what": {
                    "kind": "agent.tool",
                    "tool": tool,
                    "path": path,
                    "result_status": status,
                },
                "verdict": None,
                "dws_version": None,
                "evidence_ref": f"ws://{self.agent_id}/{self._seq}",
                "world_id": None,
                "correlation_id": correlation_id,
            }
        )

    def _cred_hash(self) -> str | None:
        import hashlib

        return (
            "sha256:" + hashlib.sha256(self.password.encode("utf-8")).hexdigest()[:40]
        )
