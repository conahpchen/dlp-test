"""Resolve the bounded DWS evidence needed by a saved scene template."""

import os
import re
from collections import defaultdict, deque

from research.grounding import anchor_relationships


MAX_ENTITIES = 6
MAX_RELATIONS = 6
DOCUMENT_PATH = re.compile(r"/kb/[a-z0-9_./-]*[a-z0-9_-]", re.I)
ENTRIES = {
    "ssh": r"(?<![a-z0-9_-])(?:ssh|cowrie)(?![a-z0-9_-])",
    "mysql": r"(?<![a-z0-9_-])(?:mysql|mysql_catalog)(?![a-z0-9_-])",
    "web": r"(?<![a-z0-9_-])(?:web|http|https|oa)(?![a-z0-9_-])|网页",
    "llm": r"(?<![a-z0-9_-])(?:llm|ai[ -]+api|openai)(?![a-z0-9_-])|模拟\s*AI",
}


class TemplateGroundingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _mentions(text, pattern):
    return any(not re.search(r"(?:不涉及|无需|不用|排除|不含|不连接|without|exclude)\s*(?:模拟)?\s*$",
                             text[:match.start()], re.I)
               for match in re.finditer(pattern, text, re.I))


def required_entries(goal):
    goal = DOCUMENT_PATH.sub("", goal)
    return tuple(entry for entry, pattern in ENTRIES.items() if _mentions(goal, pattern))


def _named_entities(entities, goal):
    named = set()
    for identifier, entity in entities.items():
        if entity["kind"] == "knowledge":
            continue
        names = (entity["label"], *entity.get("aliases", ()))
        if any(name and _mentions(goal, r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])")
               for name in names):
            named.add(identifier)
    return named


def _entry_candidates(entries, entities, relationships, named):
    choices = {}
    for entry in entries:
        if entry == "mysql":
            from entries.mysql.app import HOST

            # The MySQL entry loads catalog names from the databases hosted on its configured projection.
            candidates = [(source, (source, target, relation)) for source, target, relation in relationships
                          if relation == "hosted_on" and entities[source].get("catalog_name")
                          and entities[target]["label"] == HOST and "mysql" in entities[target].get("protocols", ())]
            explicit = {node for node in named if entities[node]["kind"] == "database"}
            if explicit:
                candidates = [item for item in candidates if item[0] in explicit]
        else:
            protocols = {"ssh": {"ssh"}, "web": {"http", "https"}, "llm": {"openai"}}[entry]
            hosts = {node for node, entity in entities.items()
                     if entity["kind"] == "host" and protocols.intersection(entity.get("protocols", ()))}
            if entry == "ssh":
                cowrie = os.environ.get("HONEYNET_COWRIE_HOST", "web01")
                hosts = {node for node in hosts if entities[node]["label"] == cowrie}
            if entry == "llm":
                gateway = os.environ.get("GATEWAY_HOST", "ai01")
                hosts = {node for node in hosts if entities[node]["label"] == gateway}
            candidates = [(node, None) for node in sorted(hosts & named or hosts)]
        if not candidates:
            raise TemplateGroundingError(f"world_scope_missing_{entry}")
        choices[entry] = candidates
    return choices


def _paths(root, neighbors):
    paths = {root: ()}
    queue = deque([root])
    while queue:
        current = queue.popleft()
        if len(paths[current]) == MAX_RELATIONS:
            continue
        for adjacent, edge in neighbors[current]:
            if adjacent not in paths:
                paths[adjacent] = (*paths[current], edge)
                queue.append(adjacent)
    return paths


def plan_scope(graph, goal, entries):
    """Select a connected, bounded set of existing relationships and its required references."""
    entities = {item["id"]: item for item in graph["entities"]}
    relationships = list(dict.fromkeys((row["source"], row["target"], row["relation"])
                                       for row in anchor_relationships(graph)))
    named = _named_entities(entities, goal)
    if len(entries) < 2 and not named:
        return None
    if len(named) > MAX_ENTITIES:
        raise TemplateGroundingError("world_scope_limit")
    choices = _entry_candidates(entries if len(entries) > 1 else (), entities, relationships, named)
    neighbors = defaultdict(list)
    for edge in relationships:
        source, target, _ = edge
        neighbors[source].append((target, edge))
        neighbors[target].append((source, edge))

    if "mysql" in choices:
        roots = dict.fromkeys(node for node, _ in choices["mysql"])
    elif named:
        roots = [min(named)]
    else:
        roots = dict.fromkeys(node for node, _ in choices[entries[0]])

    best = None
    connected = False
    for root in roots:
        paths = _paths(root, neighbors)
        if not named <= paths.keys():
            continue
        selected = {edge for node in named for edge in paths[node]}
        bindings = {}
        for entry, options in choices.items():
            reachable = [(node, proof) for node, proof in options if node in paths]
            if not reachable:
                break
            if entry == "mysql" and any(node == root for node, _ in reachable):
                reachable = [(node, proof) for node, proof in reachable if node == root]
            node, proof = min(reachable, key=lambda item: (len(paths[item[0]]), entities[item[0]]["label"], item[0]))
            selected.update(paths[node])
            if proof:
                selected.add(proof)
            bindings[entry] = node
        if len(bindings) != len(choices):
            continue
        if not selected:
            if not neighbors[root]:
                continue
            selected.add(neighbors[root][0][1])
        connected = True
        selected_ids = named | set(bindings.values()) | {root}
        selected_ids.update(node for source, target, _ in selected for node in (source, target))
        induced_count = sum(source in selected_ids and target in selected_ids
                            for source, target, _ in relationships)
        if not selected or len(selected_ids) > MAX_ENTITIES or induced_count > MAX_RELATIONS:
            continue
        score = (len(selected), len(selected_ids), entities[root]["label"], root)
        if best is None or score < best[0]:
            best = score, root, selected, selected_ids, bindings
    if best is None:
        raise TemplateGroundingError("world_scope_limit" if connected else "world_scope_disconnected")
    _, root, selected, selected_ids, bindings = best
    nodes = [entities[root], *(entities[node] for node in sorted(selected_ids - {root}))]
    edges = [dict(source=source, target=target, relation=relation)
             for source, target, relation in relationships if (source, target, relation) in selected]
    return nodes, edges, bindings, named
