"""Error Detection Agent's public entry point — what Orchestrator calls.

Streams real-time traceability onto the graph as each window completes
(not just the final fired divergences, batched after the fact): every
sub-agent's real input (which window/frames it examined) and output (its
actual reasoning text) becomes a visible actionEdge the moment that
window's analysis finishes (agents/error_detection/coordinator.py's
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

from agents.error_detection.coordinator import ErrorDetectionWindowAssessment, build_divergence_events, run_error_detection_sweep
from agents.error_detection.aggregation import DEFAULT_THRESHOLD
from agents.error_detection.severity import assess
from state import node_ids
from state.schema import DivergenceEvent, GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.state_tools import apply_state_patch, get_state_snapshot
from tools.video_utils import DEFAULT_WINDOW_S, find_video_fps, format_video_time, format_video_time_range

SUB_AGENT_LABELS = {
    "error_detection_coordinator": "Error Detection Coordinator",
    "error_detection_temporal": "Temporal Agent",
    "error_detection_spatial": "Spatial Agent",
    "error_detection_procedural": "Procedural Agent",
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
                source_tool="error_detection_case",
            ),
            reason=f"{label} registered for this case",
        )

    # Real hierarchy, not decorative: the coordinator genuinely owns these
    # three sub-agents as its real ADK sub_agents (agents/error_detection/coordinator.py),
    # invoked directly every window — this edge is what lets the graph show
    # that real structure instead of the sub-agent nodes floating unconnected
    # until their first per-window analysis edge happens to land.
    for sub_node_id, sub_label in SUB_AGENT_LABELS.items():
        if sub_node_id == "error_detection_coordinator":
            continue
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=f"edge:hierarchy-error_detection_coordinator-{sub_node_id}",
                source_node_id="agent:error_detection_coordinator",
                target_node_id=f"agent:{sub_node_id}",
                edge_kind="hierarchy",
                source_agent="error_detection_coordinator",
                source_tool="error_detection_case",
                reason=f"Error Detection Coordinator owns {sub_label}",
            ),
            reason=f"Error Detection Coordinator owns {sub_label}",
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
                source_tool="error_detection_case",
            ),
            reason=f"{label} processed window {format_video_time_range(window_start_s, window_end_s)}",
        )


async def _ensure_phase_node(case_id: str, phase: str, phases_seen: set[str], time_range_label: str) -> None:
    """Error Detection's own sub-agents reason about error categories, not general
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
    if existing is not None and existing.source_agent != "error_detection_coordinator":
        return  # a real semantic label already exists — don't clobber it with the generic fallback
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="phase",
            label=f"Phase {phase} ({time_range_label})",
            source_agent="error_detection_coordinator",
            source_tool="error_detection_case",
        ),
        reason=f"Phase {phase} first referenced",
    )


async def error_detection_case(
    case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None, window_s: float = DEFAULT_WINDOW_S
) -> list[DivergenceEvent]:
    """Runs the live Error Detection detection over [start_s, end_s) of `video_id`
    and returns every DivergenceEvent that actually fired (zero, one, or
    several per window — see module docstring).

    `stride_s=window_s` (non-overlapping windows) here — the offline
    validation sweep (scripts/run_monitor_validation_sweep.py) uses
    run_error_detection_sweep/generate_windows directly with its own dense 1s-
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

    async def on_window_complete(assessment: ErrorDetectionWindowAssessment) -> None:
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
        # ErrorDetectionWindowAssessment docstring for why only one set is surfaced
        # here even though all 6 categories were really checked.)
        for sub in assessment.sub_agent_assessments:
            await apply_state_patch(
                case_id,
                edge=GraphEdgePatch(
                    edge_id=f"edge:{sub.agent_role}-{assessment.window_id}",
                    source_node_id=f"agent:error_detection_{sub.agent_role}",
                    target_node_id=f"phase:{phase}",
                    edge_kind="detection",
                    source_agent=f"error_detection_{sub.agent_role}",
                    source_tool="run_error_detection_window",
                    reason=sub.reasoning,
                ),
                reason=sub.reasoning,
                source_agent=f"error_detection_{sub.agent_role}",
                source_tool="run_error_detection_window",
            )

        for divergence in build_divergence_events(case_id, assessment, phase):
            events.append(divergence)

            # `divergence.frame` is the analysis window's start frame (see
            # build_divergence_events) — the real frame the coordinator
            # anchors the event to, converted to the video's own real time
            # via the real fps (never assumed/hardcoded).
            video_time_s = divergence.frame / fps

            # Keyed by window and category, not by the event's own uuid — the
            # same real error re-detected on a retry must land on the SAME node
            # rather than a second one that looks like a second error.
            event_node_id = node_ids.error(divergence.window_id, divergence.error_category)

            # Severity is what Complication Reasoning filters on. Confidence
            # alone will not do: a detector can be certain about a trivial
            # error and unsure about a dangerous one.
            score, band = assess(
                divergence.error_category,
                divergence.composite_score or 0.0,
                divergence.threshold_used or DEFAULT_THRESHOLD,
            )

            readable = divergence.error_category.replace("_", " ")
            await apply_state_patch(
                case_id,
                node=GraphNodePatch(
                    node_id=event_node_id,
                    node_type="error",
                    label=f"{readable} at {format_video_time(video_time_s)}",
                    attrs={
                        "error_category": divergence.error_category,
                        "severity": score,
                        "severity_band": band,
                        "confidence": divergence.confidence,
                        "composite_score": divergence.composite_score,
                        "threshold_used": divergence.threshold_used,
                        "psi": divergence.psi,
                        "window_id": divergence.window_id,
                        "video_time_s": round(video_time_s, 1),
                        "reasoning": divergence.reasoning_trace,
                    },
                    source_agent="error_detection_coordinator",
                    source_tool="error_detection_case",
                ),
                reason=divergence.reasoning_trace,
            )
            await apply_state_patch(
                case_id,
                edge=GraphEdgePatch(
                    edge_id=node_ids.edge(f"phase:{phase}", event_node_id, "detection"),
                    source_node_id=f"phase:{phase}",
                    target_node_id=event_node_id,
                    edge_kind="detection",
                    source_agent="error_detection_coordinator",
                    source_tool="error_detection_case",
                    reason=divergence.reasoning_trace,
                ),
                reason=divergence.reasoning_trace,
            )

    await run_error_detection_sweep(
        video_id, start_s=start_s, end_s=end_s, window_s=window_s, stride_s=window_s, on_window_complete=on_window_complete
    )
    return events
