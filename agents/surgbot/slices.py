"""SurgBot-specific slice functions — same shape as tools/context_slice.py's
per-role slices (plain functions taking a GraphIndex, returning JSON-
serializable dicts, no model calls, no branching on model output).

Every SurgBot slice is a VIEW over the SAME GraphIndex tools/context_slice.py
already builds for the rest of the pipeline: it never mutates, and it reuses
GraphIndex.of_type/newest/neighbors_out/neighbors_in/snapshot_slot directly
rather than re-deriving them. active_corrective_proposals(index) is imported
from tools/context_slice.py, not reimplemented — it already encodes the
correct "acknowledged proposals stay active, only dismissed ones drop out"
rule (docs/agentic_workflow.md §11).

Real node/edge shapes below (verified this session by reading the actual
writer code, not the aspirational schema classes in state/schema.py):
  - corrective_trajectory.attrs: steps (order/action_id/action/rationale/
    verification_check/why_this_action), urgency, root_error_id,
    complication_id, rejected_action_ids, provenance, acknowledgment_outcome,
    acknowledged_at.
  - divergence_alert.attrs: proposal_id, detection_method, confidence,
    reasoning, unsatisfied_steps, advisory. proposal_id is used directly
    rather than walking the trajectory_comparison edge — both resolve to the
    same node, and attrs.proposal_id is already the plain, cheap lookup
    agents/divergence_detection/agent.py itself writes.
  - literature_evidence.attrs: pmcid, doc_id, doi, url, journal, year,
    snippet, queries_hit, sources_hit, rrf_score, retrieved_live. Linked via
    an `evidence` edge literature -> complication/corrective_trajectory
    (source is the literature node — confirmed in agents/literature_
    retrieval/agent.py), so neighbors_in(target_id, edge_kind="evidence")
    returns the literature nodes.
"""

from __future__ import annotations

from typing import Any

from tools.context_slice import GraphIndex, _node_view, _node_views, active_corrective_proposals

# --- Phase 1: case framing ---------------------------------------------------


def case_framing_slice(index: GraphIndex) -> dict[str, Any]:
    """The whole-case orientation a reviewer needs before drilling into any
    one phase: what case this is, how it went at a glance, and what's worth
    flagging for a closer look later in the script."""
    phases = sorted(index.of_type("phase"), key=lambda n: n.timestamp)
    errors = sorted(index.of_type("error"), key=lambda n: n.timestamp)
    complications = sorted(index.of_type("complication"), key=lambda n: n.timestamp)
    proposals = active_corrective_proposals(index)
    divergence_alerts = index.of_type("divergence_alert")
    benchmark = index.of_type("benchmark")

    return {
        "role": "case_framing",
        "case_id": index.snapshot.case_id,
        "trigger": _first_view(index.of_type("trigger")),
        "patient_twin": index.snapshot_slot("patient_twin"),
        "phase_count": len(phases),
        "phases": _node_views(phases),
        "error_count": len(errors),
        "recent_errors": _node_views(errors[-8:]),
        "complication_count": len(complications),
        "complications": _node_views(complications),
        "active_proposal_count": len(proposals),
        "active_proposals": _node_views(proposals),
        "divergence_alert_count": len(divergence_alerts),
        "divergence_alerts": _node_views(divergence_alerts),
        "benchmark": _first_view(benchmark),
        "documentation": _first_view(index.of_type("documentation")),
    }


# --- Phase 2: phase-by-phase walkthrough ------------------------------------


def phase_walkthrough_slice(index: GraphIndex, phase_node_id: str) -> dict[str, Any]:
    """One phase node plus everything detected during it — errors,
    complications, and any corrective activity whose root_error_id traces
    back into this phase's window. Phase nodes don't carry a direct edge to
    "errors that happened during me" today, so this slice reasons over
    timestamp adjacency (the errors immediately following this phase's own
    timestamp and preceding the next phase's), the same ordering principle
    plan_v2 §4.3 already requires of every node."""
    phase = index.nodes_by_id.get(phase_node_id)
    if phase is None:
        return {"role": "phase_walkthrough", "phase_node_id": phase_node_id, "found": False}

    all_phases = sorted(index.of_type("phase"), key=lambda n: n.timestamp)
    idx = next((i for i, p in enumerate(all_phases) if p.node_id == phase_node_id), None)
    next_phase = all_phases[idx + 1] if idx is not None and idx + 1 < len(all_phases) else None

    window_errors = [
        e
        for e in index.of_type("error")
        if e.timestamp >= phase.timestamp and (next_phase is None or e.timestamp < next_phase.timestamp)
    ]
    window_complications = [
        c
        for c in index.of_type("complication")
        if c.timestamp >= phase.timestamp and (next_phase is None or c.timestamp < next_phase.timestamp)
    ]

    return {
        "role": "phase_walkthrough",
        "phase_node_id": phase_node_id,
        "found": True,
        "phase": _node_view(phase),
        "next_phase": _node_view(next_phase) if next_phase else None,
        "errors_during_phase": _node_views(window_errors),
        "complications_during_phase": _node_views(window_complications),
    }


# --- Phase 3: error/complication chain review -------------------------------


def error_chain_slice(index: GraphIndex, error_node_id: str) -> dict[str, Any]:
    """The full causal chain hanging off one error: the error itself, the
    complication(s) it's linked to via causal_reasoning, and any literature
    evidence backing those complications — everything error_chain_reviewer
    (agents/surgbot/subagents.py) needs to produce a mechanism summary,
    plausibility probe, and citation summary without a second round trip."""
    error = index.nodes_by_id.get(error_node_id)
    if error is None:
        return {"role": "error_chain", "error_node_id": error_node_id, "found": False}

    complications = index.neighbors_out(error_node_id, edge_kind="causal_reasoning")
    literature_by_complication = {
        c.node_id: _node_views(index.neighbors_in(c.node_id, edge_kind="evidence")) for c in complications
    }
    proposals_by_complication = {
        c.node_id: _node_views(
            [p for p in index.of_type("corrective_trajectory") if p.attrs.get("complication_id") == c.node_id]
        )
        for c in complications
    }

    return {
        "role": "error_chain",
        "error_node_id": error_node_id,
        "found": True,
        "error": _node_view(error),
        "complications": _node_views(complications),
        "literature_by_complication": literature_by_complication,
        "corrective_proposals_by_complication": proposals_by_complication,
    }


# --- Phase 4: proposal + divergence review ----------------------------------


def proposal_divergence_slice(index: GraphIndex, proposal_id: str) -> dict[str, Any]:
    """One corrective proposal plus every divergence_alert raised against it
    (attrs.proposal_id, not a graph walk — the same cheap direct lookup
    agents/divergence_detection/agent.py itself writes and reads), so a
    reviewer can see the plan the system proposed alongside every point where
    the actual surgeon's course was flagged as diverging from it."""
    proposal = index.nodes_by_id.get(proposal_id)
    if proposal is None:
        return {"role": "proposal_divergence", "proposal_id": proposal_id, "found": False}

    alerts = [a for a in index.of_type("divergence_alert") if a.attrs.get("proposal_id") == proposal_id]

    return {
        "role": "proposal_divergence",
        "proposal_id": proposal_id,
        "found": True,
        "proposal": _node_view(proposal),
        "divergence_alerts": _node_views(sorted(alerts, key=lambda n: n.timestamp)),
        "acknowledgment_outcome": proposal.attrs.get("acknowledgment_outcome"),
        "acknowledged_at": proposal.attrs.get("acknowledged_at"),
    }


# --- Shared helpers ----------------------------------------------------------


def _first_view(nodes: list) -> dict[str, Any] | None:
    return _node_view(nodes[0]) if nodes else None
