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
