"""攻击者画像骨架（契约 14 §08/G8）：按 attacker_id 聚合 ATT&CK 式行为向量。"""


def build_profiles(events: list[dict], event_attacker: dict) -> dict:
    """event_attacker: {event_id: attacker_id}（来自响应或身份归并结果）。

    返回 {attacker_id: {techniques: [...], first_seen, last_seen, actions}}。
    MVP 的 technique = what.kind（后续替换为 ATT&CK 技术映射表）。
    """
    profiles: dict[str, dict] = {}
    for e in events:
        aid = event_attacker.get(e["event_id"])
        if not aid:
            continue
        p = profiles.setdefault(aid, {
            "techniques": set(), "first_seen": e["ts_hlc"],
            "last_seen": e["ts_hlc"], "actions": 0})
        p["techniques"].add((e.get("what") or {}).get("kind") or "unknown")
        if e["ts_hlc"] < p["first_seen"]:
            p["first_seen"] = e["ts_hlc"]
        if e["ts_hlc"] > p["last_seen"]:
            p["last_seen"] = e["ts_hlc"]
        p["actions"] += 1
    return {aid: {"techniques": sorted(v["techniques"]),
                  "first_seen": v["first_seen"], "last_seen": v["last_seen"],
                  "actions": v["actions"]}
            for aid, v in profiles.items()}
