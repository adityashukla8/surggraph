"""Monitor Agent's public entry point — what Orchestrator calls.

Streams real-time traceability onto the graph as each window completes
(not just the final fired divergence, batched after the fact): every
sub-agent's real input (which window/frames it examined) and output (its
actual reasoning text) becomes a visible actionEdge the moment that
window's analysis finishes (agents/monitor/coordinator.py's
`on_window_complete` callback, driven by asyncio.as_completed — see that
module for why). A fired divergence additionally gets an "event" node +
observedEdge, per plan §3.5's graph-rendering design.

Scale note: this only touches the graph for BOUNDED live-demo-segment
calls (a handful of windows) — the offline validation sweep
(scripts/run_monitor_validation_sweep.py) calls run_monitor_sweep
directly, without this wrapper, and never touches the graph (262 windows
x 3 agents would otherwise flood it).
"""

from __future__ import annotations

from agents.monitor.coordinator import MonitorWindowAssessment, build_divergence_event, run_monitor_sweep
from state.schema import DivergenceEvent, GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.state_tools import apply_state_patch
from tools.video_utils import find_video_fps

_SUB_AGENT_LABELS = {
    "monitor_coordinator": "Monitor Coordinator",
    "monitor_temporal": "Temporal Agent",
    "monitor_spatial": "Spatial Agent",
    "monitor_procedural": "Procedural Agent",
}


def _format_video_time(seconds: float) -> str:
    """m:ss, matching the native <video> player's own time display
    (VideoPanel/AnnotatedVideoPanel show "0:00 / 4:31") — so a time shown
    on the graph and a time shown on the video tiles read as the same
    clock, both derived from the real fps, never a hardcoded frame rate."""
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


async def _ensure_agent_nodes(case_id: str) -> None:
    """Persistent agent nodes — created once, not re-created per window
    (262 windows would otherwise flood the graph). Same node_id every call
    is intentional (idempotent upsert once a real state service exists)."""
    for node_id, label in _SUB_AGENT_LABELS.items():
        await apply_state_patch(
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


async def _ensure_phase_node(case_id: str, phase: str, phases_seen: set[str], time_range_label: str) -> None:
    """Real phase IDs come from the dataset's own action taxonomy
    (tools/action_labels.py), not invented — but we don't have real phase
    NAMES yet (no Scene Graph Builder to derive them, no published legend
    for this label set), so the node is honestly labeled "Phase {id}"
    rather than guessing a surgical-step name it can't back up.
    `time_range_label` is the segment's own real start-end video time (from
    ActionSegment, converted via the real fps), giving the graph the same
    timeline anchor the divergence nodes carry."""
    if phase in phases_seen:
        return
    phases_seen.add(phase)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=f"phase:{phase}",
            node_type="phase",
            label=f"Phase {phase} ({time_range_label})",
            source_agent="monitor_coordinator",
            source_tool="monitor_case",
        ),
        reason=f"Phase {phase} first referenced",
    )


async def monitor_case(case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None) -> list[DivergenceEvent]:
    """Runs the live 2-pass Monitor detection over [start_s, end_s) of
    `video_id` and returns every DivergenceEvent that actually fired.

    `async def` (no internal asyncio.run) so this can be awaited directly on
    a shared, long-lived event loop — Orchestrator's open_case() does this;
    a bare script/REPL caller should wrap it in asyncio.run() itself."""
    await _ensure_agent_nodes(case_id)
    segments = load_action_segments(video_id)
    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no source video found for {video_id!r} — cannot derive real timestamps for the graph")
    phases_seen: set[str] = set()
    events: list[DivergenceEvent] = []

    async def on_window_complete(assessment: MonitorWindowAssessment) -> None:
        phase = phase_at_frame(video_id, assessment.start_frame, segments=segments) or "unknown"
        segment = next((s for s in segments if s.start_frame <= assessment.start_frame <= s.end_frame), None)
        phase_time_range = (
            f"{_format_video_time(segment.start_frame / fps)}–{_format_video_time(segment.end_frame / fps)}"
            if segment is not None
            else "time unknown"
        )
        await _ensure_phase_node(case_id, phase, phases_seen, phase_time_range)

        # Real-time traceability: every sub-agent's actual input (this
        # window) and output (its real reasoning) becomes a visible edge,
        # whether or not the window ultimately fired a divergence — this is
        # what makes the graph work as a live trace of what each agent is
        # actually doing, not just a record of the final verdict.
        for sub in assessment.sub_agent_assessments:
            await apply_state_patch(
                case_id,
                edge=GraphEdgePatch(
                    edge_id=f"edge:{sub.agent_role}-{assessment.window_id}",
                    source_node_id=f"agent:monitor_{sub.agent_role}",
                    target_node_id=f"phase:{phase}",
                    edge_kind="action",
                    source_agent=f"monitor_{sub.agent_role}",
                    source_tool="run_monitor_window",
                    reason=sub.reasoning,
                ),
                reason=sub.reasoning,
                source_agent=f"monitor_{sub.agent_role}",
                source_tool="run_monitor_window",
            )

        if not assessment.is_divergence:
            return

        divergence = build_divergence_event(case_id, assessment, phase)
        events.append(divergence)

        # `divergence.frame` is the analysis window's start frame (see
        # build_divergence_event) — the real frame the coordinator anchors
        # the event to, converted to the video's own real time via the real
        # fps (never assumed/hardcoded; see tools/video_utils.find_video_fps).
        video_time_s = divergence.frame / fps
        event_node_id = f"event:{divergence.event_id}"
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id=event_node_id,
                node_type="event",
                label=f"Divergence: {divergence.error_category} at {_format_video_time(video_time_s)}",
                attrs={
                    "confidence": divergence.confidence,
                    "composite_score": divergence.composite_score,
                    "video_time_s": round(video_time_s, 1),
                },
                source_agent="monitor_coordinator",
                source_tool="monitor_case",
            ),
            reason=divergence.reasoning_trace,
        )
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=f"edge:{divergence.event_id}",
                source_node_id=f"phase:{phase}",
                target_node_id=event_node_id,
                edge_kind="observed",
                source_agent="monitor_coordinator",
                source_tool="monitor_case",
                reason=divergence.reasoning_trace,
            ),
            reason=divergence.reasoning_trace,
        )

    await run_monitor_sweep(video_id, start_s=start_s, end_s=end_s, on_window_complete=on_window_complete)
    return events
