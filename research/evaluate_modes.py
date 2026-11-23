"""Compare fixed, rule-based and real model adaptation on identical synthetic Web tasks."""

import json
import os
import tempfile
from pathlib import Path
from statistics import median
from time import monotonic, sleep

from fastapi.testclient import TestClient

from research.controller import adapt
from research.model import Model
from research.model_input import INTENTS, model_event


CASES = (
    ("列出数据库备份的核验结果", "database_backup"),
    ("我需要检查部署回滚的状态", "deployment_rollback"),
    ("查看当前工单的核验结果", "ticket_lookup"),
    ("我想核对变更记录", "change_lookup"),
    ("显示数据库备份的状态", "database_backup"),
    ("确认部署回滚进度", "deployment_rollback"),
    ("你好", None),
    ("打开首页", None),
)


class FixedScene:
    name = "fixed-baseline"

    def choose(self, messages):
        if messages[0]["content"].startswith("Independently review"):
            return {"accepted": True, "issues": []}
        graph = json.loads(messages[-1]["content"])["entity_graph"]
        relation = next(edge for edge in graph["relations"] if edge["relation"] == "hosted_on")
        return {
            "title": "资料核验", "entry_node": "overview", "objective": "核对记录",
            "nodes": [
                {"id": "overview", "title": "资料入口", "body": "查看运维核验记录。", "links": ["records"]},
                {"id": "records", "title": "核验记录", "body": "这里可以查看存档核验进度。", "links": []},
            ],
            "entity_links": [{"node_id": "records", "entity_id": relation[key], "relation": "reviews"}
                             for key in ("source", "target")],
            "services": [{"id": "baseline-check", "method": "GET", "path": "baseline/check", "node_id": "records",
                          "data": {"topic": "database_backup", "status": "review_ready"}}],
        }


class RuleAdapter:
    name = "rules"

    def choose(self, messages):
        if messages[0]["content"].startswith("Independently review"):
            return {"accepted": True, "issues": []}
        context = json.loads(messages[-1]["content"])
        event = next(item for item in reversed(context["events"]) if item["what"]["kind"] == "research.input")
        activity = event["what"]["activity"]
        suffix = activity.replace("_", "-")
        node = next(node for node in context["current_scene"]["nodes"] if node["id"] == "records")
        return {
            "actor_hypothesis": "unknown", "action": "continue_dialogue",
            "document_id": context["documents"][0]["id"], "reason": "Infer from an allowlisted activity.",
            "evidence_event_ids": [event["event_id"]], "response_node": "records", "expected_next_nodes": ["records"],
            "scene_patch": {"nodes": [node], "services": [
                {"id": suffix, "method": "GET", "path": "review/" + suffix, "node_id": "records",
                 "data": {"topic": activity, "status": "review_ready"}},
            ]},
        }


class MeteredModel(Model):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.reported_tokens = []
        self.reported_cost_microusd = []

    def choose(self, messages):
        if messages[0]["content"].startswith("You are the interaction"):
            request = dict(messages[0])
            request["content"] += (
                "\nFor this synthetic evaluation use a GET verification service whose data contains "
                "topic selected from " + ", ".join(sorted(set(INTENTS.values()))) +
                " based on the most recent research.input activity, and status set to review_ready. "
                "Set response_node to the node that provides this service, and preserve existing routes."
            )
            messages = [request, *messages[1:]]
        return super().choose(messages)

    def _send(self, payload):
        self.calls += 1
        data, metadata = super()._send(payload)
        usage = metadata.get("usage") or {}
        if isinstance(usage.get("total_tokens"), int):
            self.reported_tokens.append(usage["total_tokens"])
        if self.budget.input_rate and self.budget.output_rate:
            if isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
                self.reported_cost_microusd.append(
                    (usage["prompt_tokens"] * self.budget.input_rate
                     + usage["completion_tokens"] * self.budget.output_rate) / 1_000_000
                )
        return data, metadata


def _measure(client, lab, run, adapter, root):
    plan = lab.plan(run)
    outcomes = []
    for text, target in CASES:
        client.cookies.clear()
        start = monotonic()
        client.get(root + "/index", params={"message": text}).raise_for_status()
        session = client.cookies.get("research_session")
        event = next(event for event in lab.report(run)["events"][::-1]
                     if event["session"] == session and event["what"]["kind"] == "research.input")
        activity = model_event(event, plan)["what"]["activity"]
        status = "fixed"
        if adapter is not None and activity not in ("unknown", "truncated"):
            status = adapt(lab, run, session, adapter, interval=1)["status"]
        deadline = monotonic() + 8
        policy = lab.policy(run, session)
        while status == "publishing" and not policy and monotonic() < deadline:
            sleep(.05)
            policy = lab.policy(run, session)
        node = policy.get("response_node") or "records"
        options = client.get(root + "/scene/" + node).json().get("services", [])
        chosen = next((item for item in reversed(options) if item["method"] == "GET"), None)
        response = client.get(chosen["href"]) if chosen else None
        data = response.json().get("data", {}) if response is not None else {}
        success = target is not None and data.get("topic") == target and data.get("status") == "review_ready"
        outcomes.append({"task": target, "observed_activity": activity, "matched": success,
                         "adapted": bool(policy), "decision": status, "elapsed_seconds": round(monotonic()-start, 3)})
    known = [case for case in outcomes if case["task"]]
    return {
        "matched": sum(case["matched"] for case in known), "known_tasks": len(known),
        "false_adaptations": sum(case["adapted"] for case in outcomes if not case["task"]),
        "negative_controls": len(outcomes) - len(known),
        "known_task_median_seconds": median(case["elapsed_seconds"] for case in known),
        "cases": outcomes,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="dlp-compare-") as path:
        temporary = Path(path)
        os.environ.update(
            BUSINESS_STATE_FILE=str(temporary / "world.sqlite"), RESEARCH_STATE_FILE=str(temporary / "research.sqlite"),
            RESEARCH_MODEL_BUDGET_FILE=str(temporary / "model-budget.sqlite"), HONEYNET_DWS_ATOMIC="1",
            RESEARCH_UNIFIED_PUBLICATION="1", RESEARCH_ADAPTIVE_ENABLED="0", RESEARCH_AUTOMATIC_DECOYS="0",
            RESEARCH_ASYNC_ADAPTATION="0", HONEYNET_PG_DSN="",
        )
        for key in ("HONEYNET_COWRIE_EXPORT", "HONEYNET_VISITOR_HEALTH_PORT"):
            os.environ.pop(key, None)
        from business import app as visitor
        from research.api import current_lab

        model = MeteredModel()
        with TestClient(visitor.app) as client:
            lab = current_lab()
            run = lab.create(visitor.get_engine().world, "plain", model=FixedScene())["run_id"]
            lab.activate(run)
            shared = {"shared_baseline": lab.graph_version(run)}
            for name, adapter in [("fixed", None), ("rules", RuleAdapter()), ("ai", model)]:
                shared[name] = _measure(client, lab, run, adapter, f"/decoy/{run}")
            shared["ai"].update(provider=model.name, provider_calls=model.calls,
                                provider_reported_tokens=sum(model.reported_tokens),
                                responses_with_usage=len(model.reported_tokens),
                                estimated_cost_microusd_if_priced=(
                                    round(sum(model.reported_cost_microusd), 3)
                                    if model.reported_cost_microusd else None
                                ))
            print(json.dumps(shared, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
