"""Graph-at-instant context slices — docs/plan_v2_autonomous_safety_system.md §5.

At each reasoning call the caller receives a JSON snapshot of the relevant
subgraph, assembled deterministically from current graph state BEFORE the call
is dispatched. Each role gets a different slice shape tuned to what it actually
needs.

Two properties this exists to guarantee:

  1. No reasoning step ever walks the graph itself. A model asking "what else is
     going on" would need tool calls, latency, and the judgment to know what to
     ask for — instead the answer is already in its prompt, assembled by plain
     Python from real state.
  2. Assembly is deterministic. This module makes no model calls and has no
     branching on model output; the same graph produces the same slice. That is
     what keeps "the agent had genuine temporal awareness" an honest claim
     rather than a hopeful one.

Every slice is JSON-serializable and small enough to embed in a prompt. The
slice is a VIEW: it never mutates, and nothing here writes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from state.schema import GraphEdgePatch, GraphNodePatch, StateSnapshot
from tools.state_tools import get_state_snapshot

# How much history each slice carries. These are deliberately small: a slice is
# working context for one decision, not an archive. The full record lives in
# the graph and the audit log.
RECENT_ENTITY_LIMIT = 12
RECENT_ERROR_LIMIT = 8
RECENT_EVENT_LIMIT = 10
RECENT_PHASE_LIMIT = 4
DIVERGENCE_WINDOW_LOOKBACK = 6


class GraphIndex:
    """A read-only index over one snapshot.

    Built once per slice rather than re-scanning the node list per lookup —
    a long case's graph is large enough that repeated linear scans across six
    slice builders is real wasted work on the latency-critical path.
    """

    def __init__(self, snapshot: StateSnapshot) -> None:
        self.snapshot = snapshot
        self.nodes_by_id: dict[str, GraphNodePatch] = {n.node_id: n for n in snapshot.nodes}
        self.nodes_by_type: dict[str, list[GraphNodePatch]] = defaultdict(list)
        for node in snapshot.nodes:
            self.nodes_by_type[node.node_type].append(node)

        self.edges_from: dict[str, list[GraphEdgePatch]] = defaultdict(list)
        self.edges_to: dict[str, list[GraphEdgePatch]] = defaultdict(list)
        for edge in snapshot.edges:
            self.edges_from[edge.source_node_id].append(edge)
            self.edges_to[edge.target_node_id].append(edge)

    def of_type(self, node_type: str) -> list[GraphNodePatch]:
        return self.nodes_by_type.get(node_type, [])

    def newest(self, node_type: str, limit: int) -> list[GraphNodePatch]:
        """Most recent first. plan_v2 §4.3 requires every node be orderable by
        timestamp, which is exactly what makes this a stable ordering across
        node kinds rather than per-kind insertion order."""
        return sorted(self.of_type(node_type), key=lambda n: n.timestamp, reverse=True)[:limit]

    def neighbors_out(self, node_id: str, edge_kind: str | None = None) -> list[GraphNodePatch]:
        out = []
        for edge in self.edges_from.get(node_id, []):
            if edge_kind is not None and edge.edge_kind != edge_kind:
                continue
            target = self.nodes_by_id.get(edge.target_node_id)
            if target is not None:
                out.append(target)
        return out

    def neighbors_in(self, node_id: str, edge_kind: str | None = None) -> list[GraphNodePatch]:
        out = []
        for edge in self.edges_to.get(node_id, []):
            if edge_kind is not None and edge.edge_kind != edge_kind:
                continue
            source = self.nodes_by_id.get(edge.source_node_id)
            if source is not None:
                out.append(source)
        return out

    def snapshot_slot(self, slot_node_id: str) -> dict[str, Any] | None:
        node = self.nodes_by_id.get(slot_node_id)
        return _node_view(node) if node else None


def _node_view(node: GraphNodePatch) -> dict[str, Any]:
    """The prompt-facing shape of a node.

    Deliberately drops source_tool and the raw patch envelope: those are
    provenance for the graph and the trace, not information a reasoning step
    needs to do its job, and every token spent on them is a token not spent on
    the actual case.
    """
    return {
        "id": node.node_id,
        "type": node.node_type,
        "label": node.label,
        "attrs": node.attrs,
        "by": node.source_agent,
        "at": node.timestamp.isoformat(),
    }


def _node_views(nodes: list[GraphNodePatch]) -> list[dict[str, Any]]:
    return [_node_view(n) for n in nodes]


# --- Per-role slices --------------------------------------------------------
# Each takes an index (so a caller assembling several slices from one snapshot
# pays for the snapshot fetch once) and returns plain JSON-serializable data.


def perception(index: GraphIndex) -> dict[str, Any]:
    """Recently observed entities + the previous window's activity, for
    temporal continuity: the step needs to know what it just saw so the same
    instrument keeps the same stable_id across windows rather than being
    re-registered under a new name every time it reappears."""
    return {
        "role": "perception",
        "active_entities": _node_views(index.newest("entity", RECENT_ENTITY_LIMIT)),
        "previous_activity": index.snapshot_slot("snapshot:current_activity"),
        "current_phase": index.snapshot_slot("snapshot:current_phase"),
    }


def error_detection(index: GraphIndex) -> dict[str, Any]:
    """Recent error history + current phase. History matters because a
    repeated error in the same phase is a different clinical signal from an
    isolated one."""
    return {
        "role": "error_detection",
        "recent_errors": _node_views(index.newest("error", RECENT_ERROR_LIMIT)),
        "current_phase": index.snapshot_slot("snapshot:current_phase"),
    }


def complication_reasoning(index: GraphIndex, triggering_error_id: str) -> dict[str, Any]:
    """The triggering error, the patient twin, the recent vitals trend, and the
    current phase — everything needed to reason about what could go wrong
    downstream for THIS patient, rather than in general."""
    return {
        "role": "complication_reasoning",
        "triggering_error": index.snapshot_slot(triggering_error_id),
        "patient_twin": index.snapshot_slot("patient_twin"),
        "vitals_trend": index.snapshot_slot("snapshot:current_vitals_summary"),
        "recent_vitals_events": _node_views(index.newest("vitals", 3)),
        "current_phase": index.snapshot_slot("snapshot:current_phase"),
        # Errors sharing a root cause change the picture — three needle-handling
        # errors in a row is a pattern, not three independent incidents.
        "recent_errors": _node_views(index.newest("error", RECENT_ERROR_LIMIT)),
    }


def corrective_replanning(index: GraphIndex, triggering_complication_id: str) -> dict[str, Any]:
    """The fullest slice in the system: replanning proposes what a surgeon
    should do next, so it needs the entire reasoning chain that led here plus
    the live case state, and must see any proposal already active so it doesn't
    contradict one still in flight."""
    complication = index.nodes_by_id.get(triggering_complication_id)

    root_errors = index.neighbors_in(triggering_complication_id, edge_kind="causal_reasoning")
    literature = index.neighbors_in(triggering_complication_id, edge_kind="evidence")

    return {
        "role": "corrective_replanning",
        "triggering_complication": _node_view(complication) if complication else None,
        "root_errors": _node_views(root_errors),
        "supporting_literature": _node_views(literature),
        "patient_twin": index.snapshot_slot("patient_twin"),
        "vitals_trend": index.snapshot_slot("snapshot:current_vitals_summary"),
        "current_phase": index.snapshot_slot("snapshot:current_phase"),
        "recent_phases": _node_views(index.newest("phase", RECENT_PHASE_LIMIT)),
        "recent_activity": _node_views(index.newest("perception_event", RECENT_EVENT_LIMIT)),
        "active_proposals": _node_views(active_corrective_proposals(index)),
    }


def divergence_detection(index: GraphIndex, proposal_id: str, lookback: int = DIVERGENCE_WINDOW_LOOKBACK) -> dict[str, Any]:
    """The active proposal plus the last N windows of ACTUAL perception — the
    comparison is proposed-vs-actual, so both sides have to be present and
    neither may be summarized away."""
    proposal = index.nodes_by_id.get(proposal_id)
    return {
        "role": "divergence_detection",
        "active_proposal": _node_view(proposal) if proposal else None,
        "actual_recent_events": _node_views(index.newest("perception_event", lookback)),
        "actual_current_activity": index.snapshot_slot("snapshot:current_activity"),
        "actual_current_phase": index.snapshot_slot("snapshot:current_phase"),
        "actual_active_entities": _node_views(index.newest("entity", RECENT_ENTITY_LIMIT)),
    }


def documentation(index: GraphIndex) -> dict[str, Any]:
    """The entire case graph, minus benchmark nodes. The operative note is a
    narrative of everything that happened, so unlike every other slice this
    one deliberately does not filter by type or recency — the whole point is
    that the graph already contains the record. benchmark is the one
    exception: documentation now runs concurrently with benchmark_case
    (agents/orchestrator/agent.py), not after it, specifically so the note no
    longer depends on that score existing yet."""
    return {
        "role": "documentation",
        "patient_twin": index.snapshot_slot("patient_twin"),
        "phases": _node_views(sorted(index.of_type("phase"), key=lambda n: n.timestamp)),
        "errors": _node_views(sorted(index.of_type("error"), key=lambda n: n.timestamp)),
        "complications": _node_views(sorted(index.of_type("complication"), key=lambda n: n.timestamp)),
        "corrective_proposals": _node_views(sorted(index.of_type("corrective_trajectory"), key=lambda n: n.timestamp)),
        "divergence_alerts": _node_views(sorted(index.of_type("divergence_alert"), key=lambda n: n.timestamp)),
        "verification_blocks": _node_views(sorted(index.of_type("verification_block"), key=lambda n: n.timestamp)),
        "action_outcomes": _node_views(sorted(index.of_type("action_outcome"), key=lambda n: n.timestamp)),
        "literature": _node_views(index.of_type("literature_evidence")),
        "entities": _node_views(index.of_type("entity")),
    }


# --- Shared helpers ---------------------------------------------------------


def active_corrective_proposals(index: GraphIndex) -> list[GraphNodePatch]:
    """Proposals that are live: written, and not dismissed via HITL.

    A proposal the surgeon acknowledged stays ACTIVE — acknowledgment silences
    the alert path (docs/agentic_workflow.md §11), it does not retire the
    proposal, and divergence detection keeps running against it in advisory
    mode. Only an explicit dismissal takes it out of play.
    """
    return [
        node
        for node in index.of_type("corrective_trajectory")
        if node.attrs.get("acknowledgment_outcome") != "dismissed"
    ]


async def build_index(case_id: str) -> GraphIndex:
    """Fetches the live snapshot and indexes it. One await per reasoning call;
    assemble every slice that call needs from the single returned index rather
    than re-fetching per slice."""
    return GraphIndex(await get_state_snapshot(case_id))
