"""Evidence-based hypotheses, never ground-truth attacker attribution."""
from collections import defaultdict

from analysis.rules import apply_rules


def analyze(events):
    groups = defaultdict(list)
    seen = set()
    for event in events:
        if event['event_id'] not in seen:
            groups[event['session']].append(event)
            seen.add(event['event_id'])
    labels = apply_rules([e for rows in groups.values() for e in rows])
    profiles = []
    for session, rows in sorted(groups.items()):
        evidence = []
        probes = [e for e in rows if e['what'].get('probe')]
        agent_claims = [e for e in rows if e['what'].get('agent_claim')]
        beacons = [e for e in rows if e['what']['kind'] == 'research.beacon']
        if len({e['what'].get('probe') for e in probes}) >= 3:
            evidence.append(dict(indicator='multi_target_probe', event_ids=[e['event_id'] for e in probes]))
        if agent_claims:
            evidence.append(dict(indicator='untrusted_agent_self_identification', event_ids=[e['event_id'] for e in agent_claims]))
        if beacons:
            evidence.append(dict(indicator='canary_follow_observed', event_ids=[e['event_id'] for e in beacons]))
        ids = {e['event_id'] for e in rows}
        behavior_labels = [label for label in labels if label['event_id'] in ids]
        for label in behavior_labels:
            evidence.append(dict(indicator=label['class'], event_ids=[label['event_id']]))
        hypothesis = 'unknown'
        if any(e['indicator'] in ('multi_target_probe', 'service_discovery') for e in evidence):
            hypothesis = 'scanner_suspected'
        elif agent_claims:
            hypothesis = 'ai_agent_suspected'
        profiles.append(dict(session=session, hypothesis=hypothesis,
                             confidence='low' if hypothesis != 'unknown' else 'undetermined',
                             evidence=evidence, actions=len(rows),
                             limitations=['Headers and links can be copied or spoofed; human/AI identity is unverified.',
                                          'A canary request proves HTTP access only, not command execution.']))
    return profiles


AGENT_SIGNALS = frozenset({
    "untrusted_agent_self_identification",
    "multi_target_probe",
    "service_discovery",
})


def session_agent_assessments(events, decisions):
    """Read retained evidence without combining sessions or requesting a fresh model judgment."""
    sessions = defaultdict(dict)
    for event in events:
        sessions[event["session"]][event["event_id"]] = event
    latest = {row["session"]: row.get("decision") or {} for row in decisions if row["status"] == "applied"}

    assessments = {}
    for session, observed in sessions.items():
        # The general scanner rule can aggregate by source network. Limit it to this session here.
        profile, = analyze(list(observed.values()))
        signals = defaultdict(set)
        for item in profile["evidence"]:
            if item["indicator"] in AGENT_SIGNALS:
                signals[item["indicator"]].update(item["event_ids"])
        decision = latest.get(session, {})
        citations = set(decision.get("evidence_event_ids") or ())
        model_suspects_agent = bool(citations and citations <= observed.keys()
                                    and decision.get("actor_hypothesis") == "ai_agent_suspected")
        if model_suspects_agent:
            signals["model_agent_hypothesis"] = citations
        assessments[session] = {
            "suspected": model_suspects_agent,
            "signals": {kind: sorted(ids, reverse=True) for kind, ids in signals.items()},
        }
    return assessments
