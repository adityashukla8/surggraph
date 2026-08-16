"""Validates a case's graph CHAIN — what connects to what — not just its contents.

    uv run scripts/validate_graph_chain.py <case_id> [--json]

WHY THIS EXISTS. The store has no foreign-key enforcement and the renderer
drops a dangling edge silently, so a graph can be badly broken for a viewer
while every write "succeeded" and every node count looks right. Two real bugs
shipped that way: perception output rendered as a field of disconnected nodes
with no path back to the agent that produced it, and Error Detection wrote
detection edges to `phase:{id}` while Perception wrote `phase:{id}:{window}`,
so every one of them was discarded without a word.

The chain is what lets an end user understand the graph. It is also literally
what the Documentation Agent will read at case close, and what every context
slice is carved out of — so if the chain is wrong, the reasoning built on it is
wrong too, whatever the node counts say.

WHAT IT CHECKS
  1. Dangling edges     — an endpoint that no node in the graph provides.
  2. Orphan nodes       — no edge touches them at all.
  3. Reachability       — everything reachable from the trigger node.
  4. Expected relations — the reasoning chain matches
                          docs/plan_v2_autonomous_safety_system.md §4.2/§6.

Exits non-zero if any structural check fails, so it can gate a real run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

# What the functional plan says must be connected, and by which edge kind.
# Each entry: (source node_type, edge_kind, target node_type, why it matters).
# A relation is only checked when a node of the source kind actually exists —
# the point is to catch a chain that is WRONG, not to demand agents that have
# not been built yet.
EXPECTED_RELATIONS = [
    ("trigger", "hierarchy", "agent", "every agent hangs off the case-open trigger"),
    ("trigger", "hierarchy", "patient_twin", "the patient twin is part of the static skeleton"),
    ("agent", "hierarchy", "phase", "the perceiving agent owns the first activity it observed"),
    ("phase", "succession", "phase", "activities form a chronological chain (§4.3)"),
    ("phase", "hierarchy", "perception_event", "an event belongs to the activity it happened during"),
    ("perception_event", "involved", "entity", "events reference entities, never copy them (§7.3)"),
    ("agent", "detection", "error", "a detected error traces back to its detector"),
    ("error", "causal_reasoning", "complication", "error -> complication (§4.2)"),
    ("literature_evidence", "evidence", "complication", "a complication is grounded in retrieved literature"),
    ("complication", "proposal", "corrective_trajectory", "a corrective proposal answers a complication"),
    ("corrective_trajectory", "trajectory_comparison", "divergence_alert", "divergence is measured against the proposal"),
    ("error", "hierarchy", "literature_evidence", "papers hang off the error whose investigation retrieved them"),
    ("action_intent", "verification", "verification_block", "every external write passes the fail-closed gate"),
    ("action_intent", "outcome", "action_outcome", "an intent records what really happened"),
]


def fetch(case_id: str, state_service_url: str) -> dict:
    resp = httpx.get(f"{state_service_url}/state/{case_id}/snapshot", timeout=30)
    resp.raise_for_status()
    return resp.json()


def analyze(snapshot: dict) -> dict:
    nodes = {n["node_id"]: n for n in snapshot["nodes"]}
    edges = snapshot["edges"]

    dangling = [
        e for e in edges if e["source_node_id"] not in nodes or e["target_node_id"] not in nodes
    ]
    touched = {x for e in edges for x in (e["source_node_id"], e["target_node_id"])}
    orphans = [nid for nid in nodes if nid not in touched]

    # Reachability from the trigger, following edges in BOTH directions: the
    # question is "is this node part of the case's connected story", and some
    # real relations legitimately point inward (literature -> complication).
    adjacency: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e["source_node_id"] in nodes and e["target_node_id"] in nodes:
            adjacency[e["source_node_id"]].add(e["target_node_id"])
            adjacency[e["target_node_id"]].add(e["source_node_id"])

    # Only meaningful when a real root exists. A case built by calling one
    # agent directly (a focused test) legitimately has no trigger node, and
    # calling every node "unreachable" there would be a false alarm that
    # trains you to ignore this check.
    roots = [nid for nid, n in nodes.items() if n["node_type"] == "trigger"]
    reachable: set[str] = set()
    queue = deque(roots)
    while queue:
        nid = queue.popleft()
        if nid in reachable:
            continue
        reachable.add(nid)
        queue.extend(adjacency[nid] - reachable)

    unreachable = [nid for nid in nodes if nid not in reachable] if roots else []

    present_kinds = {n["node_type"] for n in nodes.values()}
    observed = {
        (nodes[e["source_node_id"]]["node_type"], e["edge_kind"], nodes[e["target_node_id"]]["node_type"])
        for e in edges
        if e["source_node_id"] in nodes and e["target_node_id"] in nodes
    }

    kind_counts = Counter(n["node_type"] for n in nodes.values())

    relations = []
    for src, kind, tgt, why in EXPECTED_RELATIONS:
        # A self-relation (phase -> phase) needs at least two of that kind to
        # be possible at all. One activity observed so far is a case still
        # early, not a broken chain — flagging it would be a false alarm.
        needed = 2 if src == tgt else 1
        if kind_counts.get(src, 0) < needed or kind_counts.get(tgt, 0) < 1:
            status = "n/a"  # not yet possible — not a failure
        elif (src, kind, tgt) in observed:
            status = "ok"
        else:
            status = "MISSING"
        relations.append({"source": src, "edge": kind, "target": tgt, "why": why, "status": status})

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_kinds": dict(Counter(n["node_type"] for n in nodes.values())),
        "edge_kinds": dict(Counter(e["edge_kind"] for e in edges)),
        "dangling": [
            {"edge_id": e["edge_id"], "kind": e["edge_kind"], "source": e["source_node_id"], "target": e["target_node_id"]}
            for e in dangling
        ],
        "orphans": [{"node_id": n, "type": nodes[n]["node_type"], "label": nodes[n]["label"]} for n in orphans],
        "unreachable": [{"node_id": n, "type": nodes[n]["node_type"], "label": nodes[n]["label"]} for n in unreachable],
        "relations": relations,
        "has_trigger": bool(roots),
    }


def print_chain(snapshot: dict) -> None:
    """The activity spine — what a viewer actually follows across the case."""
    nodes = {n["node_id"]: n for n in snapshot["nodes"]}
    out: dict[str, list] = defaultdict(list)
    for e in snapshot["edges"]:
        out[e["source_node_id"]].append(e)

    phases = sorted(
        (n for n in nodes.values() if n["node_type"] == "phase"), key=lambda n: n["timestamp"]
    )
    if not phases:
        print("  (no activity nodes yet)")
        return

    for phase in phases:
        print(f"  ▸ {phase['label'][:70]}")
        children = [
            nodes[e["target_node_id"]]
            for e in out.get(phase["node_id"], [])
            if e["target_node_id"] in nodes and nodes[e["target_node_id"]]["node_type"] != "phase"
        ]
        for child in children:
            print(f"      · [{child['node_type']}] {child['label'][:62]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--state-service-url", default=os.environ.get("STATE_SERVICE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    snapshot = fetch(args.case_id, args.state_service_url)
    result = analyze(snapshot)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"=== {args.case_id} ===")
        print(f"  {result['node_count']} nodes, {result['edge_count']} edges")
        print(f"  node kinds: {result['node_kinds']}")
        print(f"  edge kinds: {result['edge_kinds']}")

        print("\n--- Structural integrity ---")
        for name, key in (("dangling edges", "dangling"), ("orphan nodes", "orphans"), ("unreachable from trigger", "unreachable")):
            items = result[key]
            print(f"  {name}: {len(items)}")
            for item in items[:8]:
                print(f"      {item}")
        if not result["has_trigger"]:
            print("  NOTE: no trigger node in this case — reachability skipped (not counted as a failure).")
            print("        Expected when an agent was driven directly rather than through the orchestrator.")

            print("\n--- Chain coverage (docs/plan_v2 §4.2/§6) ---")
        print("  Informational, not a gate: a relation can be absent because the agent")
        print("  is not built yet, or because it honestly had nothing to assert — an")
        print("  evidence edge cannot form when no retrieved paper actually supports")
        print("  the claim, and inventing one would be worse than the gap.")
        for rel in result["relations"]:
            mark = {"ok": "  ok  ", "MISSING": " MISS ", "n/a": "  --  "}[rel["status"]]
            print(f"  [{mark}] {rel['source']} --{rel['edge']}--> {rel['target']}")
            if rel["status"] == "MISSING":
                print(f"           expected because: {rel['why']}")

        print("\n--- Activity spine ---")
        print_chain(snapshot)

    # Unreachable only counts when there was a root to be reachable from.
    # Only genuine structural breakage gates. A dangling edge or an orphan node
    # is always a bug — nothing legitimately produces one. A missing expected
    # relation is not: it can mean the agent is unbuilt, or that it correctly
    # had nothing to assert. Gating on it would make this check cry wolf on
    # every run, which trains you to stop reading it.
    failures = len(result["dangling"]) + len(result["orphans"]) + len(result["unreachable"])
    missing = sum(1 for r in result["relations"] if r["status"] == "MISSING")
    if failures:
        print(f"\nFAILED: {failures} structural problem(s) — dangling edges / orphans / unreachable", file=sys.stderr)
        return 1
    if missing:
        print(f"\nPASSED structurally. {missing} expected relation(s) not present — see coverage above.")
        return 0
    print("\nPASSED: chain fully connected, every applicable relation present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
