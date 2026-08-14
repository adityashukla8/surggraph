"""Monitor Agent's public entry point — what Orchestrator calls.

Streams real-time traceability onto the graph as each window completes
(not just the final fired divergences, batched after the fact): every
sub-agent's real input (which window/frames it examined) and output (its
actual reasoning text) becomes a visible actionEdge the moment that
window's analysis finishes (agents/monitor/coordinator.py's
`on_window_complete` callback, driven by asyncio.as_completed — see that
module for why). A window can now fire MULTIPLE real divergences (one per
error category that independently crossed threshold — see
docs/latency_optimization.md's unconditional-deep-pass restructuring),
each getting its own "event" node + observedEdge.

Every node this module writes carries a real video-time range, widened as
more windows land for agent nodes (the persistent ones aren't tied to one
moment, so their range is the real cumulative span of windows they've
actually processed) — not just phase/event nodes as before.
"""

from __future__ import annotations

from agents.monitor.coordinator import MonitorWindowAssessment, build_divergence_events, run_monitor_sweep
from state.schema import DivergenceEvent, GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.state_tools import apply_state_patch, get_state_snapshot
from tools.video_utils import find_video_fps, format_video_time, format_video_time_range

SUB_AGENT_LABELS = {
    "monitor_coordinator": "Monitor Coordinator",
    "monitor_temporal": "Temporal Agent",
    "monitor_spatial": "Spatial Agent",
    "monitor_procedural": "Procedural Agent",
}


async def _ensure_agent_nodes(case_id: str) -> None:
    """Persistent agent nodes — created once, no time range yet (real
    windows haven't landed). Same node_id every call is intentional
    (idempotent upsert)."""
    for node_id, label in SUB_AGENT_LABELS.items():
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

    # Real hierarchy, not decorative: the coordinator genuinely owns these
    # three sub-agents as its real ADK sub_agents (agents/monitor/coordinator.py),
    # invoked directly every window — this edge is what lets the graph show
    # that real structure instead of the sub-agent nodes floating unconnected
    # until their first per-window analysis edge happens to land.
    for sub_node_id, sub_label in SUB_AGENT_LABELS.items():
        if sub_node_id == "monitor_coordinator":
            continue
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=f"edge:hierarchy-monitor_coordinator-{sub_node_id}",
                source_node_id="agent:monitor_coordinator",
                target_node_id=f"agent:{sub_node_id}",
                edge_kind="action",
                source_agent="monitor_coordinator",
                source_tool="monitor_case",
                reason=f"Monitor Coordinator owns {sub_label}",
            ),
            reason=f"Monitor Coordinator owns {sub_label}",
        )


async def _widen_agent_time_ranges(
    case_id: str, window_start_s: float, window_end_s: float, agent_ranges: dict[str, tuple[float, float]]
) -> None:
    """Widens (never shrinks) each persistent agent node's real cumulative
    time range to cover this window too, re-writing the node's label. Runs
    once per window — real, not decorative: an agent node's range is
    exactly the real span of windows it has actually processed so far."""
    for node_id, label in SUB_AGENT_LABELS.items():
        prev = agent_ranges.get(node_id)
        new_range = (window_start_s, window_end_s) if prev is None else (min(prev[0], window_start_s), max(prev[1], window_end_s))
        agent_ranges[node_id] = new_range
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id=f"agent:{node_id}",
                node_type="agent",
                label=f"{label} ({format_video_time_range(*new_range)})",
                source_agent=node_id,
                source_tool="monitor_case",
            ),
            reason=f"{label} processed window {format_video_time_range(window_start_s, window_end_s)}",
        )


async def _ensure_phase_node(case_id: str, phase: str, phases_seen: set[str], time_range_label: str) -> None:
    """Monitor's own sub-agents reason about error categories, not general
    phase identity — it has no real semantic description of its own to
    offer for a phase node's label (unlike Scene Graph Builder's real
    `activity_description` or Anticipation's own live naming, plan §13.4).
    So this only ever CREATES the node if nothing better already exists —
    it must never clobber a real semantic label with the generic
    `"Phase {id}"` fallback, a real race given Scene Graph Builder/
    Anticipation run concurrently against the same node_id. One real
    snapshot read per NEWLY-seen phase (gated by `phases_seen`, so at most
    a handful of times per video, never per window) — negligible against
    this agent's own per-window Gemini call volume."""
    if phase in phases_seen:
        return
    phases_seen.add(phase)
    node_id = f"phase:{phase}"
    snapshot = await get_state_snapshot(case_id)
    existing = next((n for n in snapshot.nodes if n.node_id == node_id), None)
    if existing is not None and existing.source_agent != "monitor_coordinator":
        return  # a real semantic label already exists — don't clobber it with the generic fallback
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="phase",
            label=f"Phase {phase} ({time_range_label})",
            source_agent="monitor_coordinator",
            source_tool="monitor_case",
        ),
        reason=f"Phase {phase} first referenced",
    )


async def monitor_case(
    case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None, window_s: float = 10.0
) -> list[DivergenceEvent]:
    """Runs the live Monitor detection over [start_s, end_s) of `video_id`
    and returns every DivergenceEvent that actually fired (zero, one, or
    several per window — see module docstring).

    `stride_s=window_s` (non-overlapping windows) here — the offline
    validation sweep (scripts/run_monitor_validation_sweep.py) uses
    run_monitor_sweep/generate_windows directly with its own dense 1s-
    stride CARES grid (~262 windows for a 271s video) to score macro-F1
    against per-second ground truth; that grid is real for measuring
    accuracy but far too expensive to run live (~21 real Gemini calls per
    window x 262 windows). The live path instead advances one window per
    real window_s of video, matching the pipelined "window N+1 starts once
    window N's real video-time has elapsed" cadence — full video coverage
    without the offline sweep's redundant overlap.

    `async def` (no internal asyncio.run) so this can be awaited directly on
    a shared, long-lived event loop — Orchestrator's open_case() does this;
    a bare script/REPL caller should wrap it in asyncio.run() itself."""
    await _ensure_agent_nodes(case_id)
    segments = load_action_segments(video_id)
    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no source video found for {video_id!r} — cannot derive real timestamps for the graph")
    phases_seen: set[str] = set()
    agent_ranges: dict[str, tuple[float, float]] = {}
    events: list[DivergenceEvent] = []

    async def on_window_complete(assessment: MonitorWindowAssessment) -> None:
        phase = phase_at_frame(video_id, assessment.start_frame, segments=segments) or "unknown"
        segment = next((s for s in segments if s.start_frame <= assessment.start_frame <= s.end_frame), None)
        phase_time_range = (
            format_video_time_range(segment.start_frame / fps, segment.end_frame / fps) if segment is not None else "time unknown"
        )
        await _ensure_phase_node(case_id, phase, phases_seen, phase_time_range)

        window_start_s = assessment.start_frame / fps
        window_end_s = assessment.end_frame / fps
        await _widen_agent_time_ranges(case_id, window_start_s, window_end_s, agent_ranges)

        # Real-time traceability: every sub-agent's actual input (this
        # window) and output (its real reasoning) becomes a visible edge,
        # whether or not the window ultimately fired a divergence — this is
        # what makes the graph work as a live trace of what each agent is
        # actually doing, not just a record of the final verdict. (The
        # "top" category's assessments — see coordinator.py's
        # MonitorWindowAssessment docstring for why only one set is surfaced
        # here even though all 6 categories were really checked.)
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

        for divergence in build_divergence_events(case_id, assessment, phase):
            events.append(divergence)

            # `divergence.frame` is the analysis window's start frame (see
            # build_divergence_events) — the real frame the coordinator
            # anchors the event to, converted to the video's own real time
            # via the real fps (never assumed/hardcoded).
            video_time_s = divergence.frame / fps
            event_node_id = f"event:{divergence.event_id}"
            await apply_state_patch(
                case_id,
                node=GraphNodePatch(
                    node_id=event_node_id,
                    node_type="event",
                    label=f"Divergence: {divergence.error_category} at {format_video_time(video_time_s)}",
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

    await run_monitor_sweep(
        video_id, start_s=start_s, end_s=end_s, window_s=window_s, stride_s=window_s, on_window_complete=on_window_complete
    )
    return events
