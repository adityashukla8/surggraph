"""Monitor Agent's public entry point — what Orchestrator calls.

Wraps the coordinator's `run_monitor_sweep` (agents/monitor/coordinator.py)
with: real per-window phase lookup (tools/action_labels.py), graph-node/edge
emission for fired divergences (tools/state_tools.py — the persistent
Temporal/Spatial/Procedural/coordinator agent nodes are created once, and
each real divergence gets an "event" node + observedEdge, per plan §3.5's
graph-rendering design), and building the final DivergenceEvent list.
"""

from __future__ import annotations

import asyncio

from agents.monitor.coordinator import build_divergence_event, run_monitor_sweep
from state.schema import DivergenceEvent, GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.state_tools import apply_state_patch

_SUB_AGENT_LABELS = {
    "monitor_coordinator": "Monitor Coordinator",
    "monitor_temporal": "Temporal Agent",
    "monitor_spatial": "Spatial Agent",
    "monitor_procedural": "Procedural Agent",
}


def _ensure_agent_nodes(case_id: str) -> None:
    """Persistent agent nodes — created once, not re-created per window
    (262 windows would otherwise flood the graph). Same node_id every call
    is intentional (idempotent upsert once a real state service exists)."""
    for node_id, label in _SUB_AGENT_LABELS.items():
        apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id=f"agent:{node_id}",
                node_type="agent",
                label=label,
                source_agent=node_id,
                source_tool="monitor_case",
            ),
            reason=f"{label} registered for this case",
        )


def monitor_case(case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None) -> list[DivergenceEvent]:
    """Runs the live 2-pass Monitor detection over [start_s, end_s) of
    `video_id` and returns every DivergenceEvent that actually fired.
    Emits graph patches for the fired windows only (per plan §3.5 — the
    offline validation sweep, by contrast, never touches the graph)."""
    _ensure_agent_nodes(case_id)

    assessments = asyncio.run(run_monitor_sweep(video_id, start_s=start_s, end_s=end_s))
    segments = load_action_segments(video_id)

    events: list[DivergenceEvent] = []
    for assessment in assessments:
        if not assessment.is_divergence:
            continue

        phase = phase_at_frame(video_id, assessment.start_frame, segments=segments) or "unknown"
        divergence = build_divergence_event(case_id, assessment, phase)
        events.append(divergence)

        event_node_id = f"event:{divergence.event_id}"
        apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id=event_node_id,
                node_type="event",
                label=f"Divergence: {divergence.error_category}",
                attrs={"confidence": divergence.confidence, "composite_score": divergence.composite_score},
                source_agent="monitor_coordinator",
                source_tool="monitor_case",
            ),
            reason=divergence.reasoning_trace,
        )
        apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=f"edge:{divergence.event_id}",
                source_node_id=f"phase:{phase}",
                target_node_id=event_node_id,
                edge_kind="observed",
                source_agent="monitor_coordinator",
                source_tool="monitor_case",
            ),
            reason=divergence.reasoning_trace,
        )

    return events
