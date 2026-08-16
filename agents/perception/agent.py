"""Perception Sweep Agent — the public entry point Orchestrator calls.

docs/agentic_workflow.md §2 agent 1. One reasoning call per ~5s window for the
case duration, feeding a deterministic change-diff layer that decides what the
graph actually hears about.

THE SHAPE OF ONE WINDOW:

    sample stills  ->  Gemini call  ->  raw output
                                          |
                            +-------------+-------------+
                            |                           |
                     audit log (all of it)      change-diff + debounce
                                                        |
                                          entity registry (in place)
                                          snapshot slots  (in place)
                                          event stream    (only on change)

Steady state is SILENT. If nothing meaningful changed, this writes entity
counters and snapshot slots in place and emits no events at all — which is
what keeps downstream event-driven agents from firing on noise, and what keeps
the graph readable over a full case.

Windows are strictly sequential, unlike Error Detection's fan-out. The
change-diff layer is inherently ordered — window N's decisions depend on
window N-1's accumulated state — so processing them concurrently would be
meaningless even if it were faster. The Gemini call is the long pole, and
overlapping windows would race the very state the diff reads.
"""

from __future__ import annotations

import asyncio
import logging

from agents.perception.pipeline import PerceptionPipeline, WindowObservation
from agents.perception.subagent import PerceptionWindowOutput, build_subagent
from agents.perception.writer import (
    SOURCE_AGENT,
    entity_patch,
    event_patches,
    link,
    phase_patch,
    snapshot_patch,
    write_audit_record,
)
from state import node_ids
from state.schema import GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.adk_runner import run_llm_agent_once
from tools.context_slice import build_index, perception as perception_slice
from tools.state_tools import apply_state_patch, apply_state_patches
from tools.vitals_stream import sample_at, vitals_patch
from tools.video_utils import (
    DEFAULT_WINDOW_S,
    VideoWindow,
    build_multimodal_content,
    find_video_fps,
    find_video_path,
    generate_nonoverlapping_windows,
    sample_frames,
)

logger = logging.getLogger(__name__)

AGENT = build_subagent()

_GEMINI_CONCURRENCY = asyncio.Semaphore(4)

# Native resolution, no resize: naming an instrument and reading a relation
# both depend on fine detail that a downscale removes. Error Detection's
# temporal role can afford 960x540 because it is reading motion trend, not
# identity.
_STILL_FRAME_COUNT = 5
_STILL_RESIZE = None

_SOURCE_TOOL = "perception_case"


async def _ensure_agent_node(case_id: str) -> None:
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_ids.agent(SOURCE_AGENT),
            node_type="agent",
            label="Perception Agent",
            source_agent=SOURCE_AGENT,
            source_tool=_SOURCE_TOOL,
        ),
        reason="Perception Agent registered for this case",
    )


async def _perceive_window(video_id: str, window: VideoWindow, slice_context: dict, phase_hint: str | None) -> PerceptionWindowOutput:
    video_path = find_video_path(video_id)
    if video_path is None:
        raise FileNotFoundError(f"no source video found for {video_id!r}")

    frames = sample_frames(
        video_path, window.start_frame, window.end_frame, n_frames=_STILL_FRAME_COUNT, resize_to=_STILL_RESIZE
    )

    # The previous window's active entity ids go in verbatim — this is what
    # makes stable-id reuse possible at all. Without it the model has no way
    # to know what it called something 5 seconds ago.
    known = [e["id"].removeprefix("entity:") for e in slice_context["active_entities"] if e["attrs"].get("is_active")]
    previous_activity = (slice_context.get("previous_activity") or {}).get("label")

    lines = [f"Current window: video seconds {window.start_s:.1f}-{window.end_s:.1f}."]
    if known:
        lines.append(f"Entity ids already active in this case (REUSE THESE EXACTLY if you see them again): {', '.join(sorted(known))}")
    else:
        lines.append("No entities registered yet in this case — you are establishing the initial ids.")
    if previous_activity:
        lines.append(f"Previous window's activity: {previous_activity}")
    if phase_hint is not None:
        lines.append(f"Upstream classifier phase id (opaque, no semantic name — a hint only): {phase_hint}")

    content = build_multimodal_content("\n".join(lines), frames)
    async with _GEMINI_CONCURRENCY:
        return await run_llm_agent_once(AGENT, content, PerceptionWindowOutput, app_name="surggraph_perception")


async def _emit(case_id: str, pipeline: PerceptionPipeline, decision, obs: WindowObservation, vitals, spine: dict) -> int:
    """Applies one window's decision to the graph as a SINGLE batched write.

    Batched rather than written one at a time because writes to one case
    serialize at roughly a second each (measured), so a window emitting ten
    patches individually would cost ~10s against a 5s cadence. Batched, it is
    one round trip — measured 5x faster on ten patches.

    Ordering within the batch is load-bearing: entities go first, because an
    event's `involved` edge points at an entity node and the store assigns
    consecutive seqs in list order. An edge landing before its endpoint would
    be dropped by the renderer.
    """
    patches: list[tuple] = [entity_patch(state, _SOURCE_TOOL) for state in decision.entity_updates]

    written = 0
    pending_event_ids: list[str] = []
    for event in decision.events:
        seq = pipeline.next_seq()
        patches.extend(event_patches(event, seq, obs, _SOURCE_TOOL))
        pending_event_ids.append(node_ids.perception_event(seq, event.kind))
        written += 1

    active = sorted(pipeline.active_entity_ids())
    patches.append(
        snapshot_patch(
            node_ids.SNAPSHOT_ACTIVE_ENTITY_SET,
            f"{len(active)} entities in view",
            {"active_entity_ids": [node_ids.entity(e) for e in active], "window_index": obs.window_index},
            _SOURCE_TOOL,
        )
    )

    if decision.activity_changed_to:
        patches.append(
            snapshot_patch(
                node_ids.SNAPSHOT_CURRENT_ACTIVITY,
                decision.activity_changed_to,
                {"window_index": obs.window_index, "video_time_s": round(obs.video_time_s, 1)},
                _SOURCE_TOOL,
            )
        )

    if decision.phase_changed_to is not None:
        label = pipeline.current_activity_text or f"Phase segment from {obs.video_time_s:.0f}s"
        phase_node_id = node_ids.phase(decision.phase_changed_to, obs.window_index)
        patches.append(phase_patch(decision.phase_changed_to, obs.window_index, label, _SOURCE_TOOL))

        # The chronological spine (§4.3). The first activity hangs off the
        # agent that perceived it; every later one follows its predecessor, so
        # the graph reads left-to-right as the case actually unfolded rather
        # than as a scatter of timestamped nodes.
        if spine["previous_phase_node_id"] is None:
            patches.append(
                link(node_ids.agent(SOURCE_AGENT), phase_node_id, "hierarchy", f"First activity observed: {label}", _SOURCE_TOOL)
            )
        else:
            patches.append(
                link(spine["previous_phase_node_id"], phase_node_id, "succession", f"Followed by: {label}", _SOURCE_TOOL)
            )
        spine["previous_phase_node_id"] = phase_node_id

        patches.append(
            snapshot_patch(
                node_ids.SNAPSHOT_CURRENT_PHASE,
                label,
                {
                    "phase_node_id": node_ids.phase(decision.phase_changed_to, obs.window_index),
                    "opaque_phase_id": decision.phase_changed_to,
                    "window_index": obs.window_index,
                },
                _SOURCE_TOOL,
            )
        )

    # Vitals ride in the same batch (docs/plan_v2 §6 step 2). The snapshot slot
    # updates every window — it is fixed-cardinality, so that is free — but a
    # physiological-state NODE is written only on a real deviation. One per
    # window would flood the graph with the steady-state noise the change-diff
    # design exists to suppress; across a real 55-window sweep only ~22% of
    # windows deviate.
    patches.append(
        snapshot_patch(
            node_ids.SNAPSHOT_CURRENT_VITALS,
            f"Vitals: HR {vitals.hr_bpm:.0f} · MAP {vitals.map_mmhg:.0f} · SpO2 {vitals.spo2_pct:.0f}% (synthetic)",
            {"synthetic": True, **vitals.to_dict()},
            _SOURCE_TOOL,
        )
    )
    if vitals.deviations or vitals.is_excursion:
        patches.append(vitals_patch(vitals, obs.window_index))
        # Anchored like any other observation: a physiological deviation is
        # something that happened DURING an activity, and leaving it floating
        # would strip exactly the context that makes it interpretable.
        pending_event_ids.append(node_ids.vitals(obs.window_index))
        written += 1

    # Every event belongs to the activity it happened during. Without this an
    # event node has no path back to the agent that produced it and renders as
    # an orphan, which is exactly what the graph is supposed to prevent.
    anchor = spine["previous_phase_node_id"] or node_ids.agent(SOURCE_AGENT)
    for event_node_id in pending_event_ids:
        patches.append(link(anchor, event_node_id, "hierarchy", "Observed during this activity", _SOURCE_TOOL))

    # The snapshot slots are the agent's live view of the case, so they hang off
    # it rather than floating. Linked in the SAME batch that first creates them,
    # not up front: a slot node only exists once a window has something to put
    # in it, and an edge written before its endpoint dangles until then — which
    # is exactly what the chain validator caught. Batch order guarantees the
    # node lands before the edge, since the store assigns seqs in list order.
    slots_in_this_batch = {
        node.node_id for node, _edge, _reason in patches if node is not None and node.node_type == "snapshot"
    }
    for slot in sorted(slots_in_this_batch - spine["slots_linked"]):
        patches.append(link(node_ids.agent(SOURCE_AGENT), slot, "hierarchy", "Live case state", _SOURCE_TOOL))
        spine["slots_linked"].add(slot)

    await apply_state_patches(case_id, patches)
    return written


async def perception_case(
    case_id: str,
    video_id: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    window_s: float = DEFAULT_WINDOW_S,
) -> PerceptionPipeline:
    """Runs the perception sweep over [start_s, end_s). Returns the pipeline so
    a caller can inspect the final registry state."""
    await _ensure_agent_node(case_id)

    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no real fps available for {video_id!r}")
    if end_s is None:
        raise ValueError("end_s is required — derive it from the real video duration, never assume one")

    case_duration_s = end_s
    windows = generate_nonoverlapping_windows(start_s, end_s, window_s, fps, id_prefix="perception")

    # Carries the tail of the activity chain across windows. A dict rather than
    # a local so _emit can advance it.
    spine: dict = {"previous_phase_node_id": None, "slots_linked": set()}


    try:
        segments = load_action_segments(video_id)
    except FileNotFoundError:
        # The upstream phase signal is a hint, not a dependency. Its absence
        # costs the model one line of context; it must never stop perception.
        logger.warning("perception[%s]: no action annotations for %s — running without the phase hint", case_id, video_id)
        segments = None

    pipeline = PerceptionPipeline()
    logger.info("perception[%s]: sweeping %d windows of %.1fs", case_id, len(windows), window_s)

    for window_index, window in enumerate(windows):
        index = build_index(case_id)
        slice_context = perception_slice(await index)

        # The opaque numeric phase id: internal bookkeeping and a prompt hint,
        # never a semantic answer handed to the model.
        phase_hint = phase_at_frame(video_id, window.start_frame, segments=segments) if segments else None

        try:
            raw = await _perceive_window(video_id, window, slice_context, phase_hint)
        except Exception:
            # A failed window emits nothing. Downstream sees SILENCE, not an
            # error — which is the correct signal, since "we could not tell"
            # is not "nothing happened" but it is also not a graph event.
            # Snapshot slots keep their previous values (docs §10).
            logger.exception("perception[%s]: window %s failed, emitting nothing", case_id, window.window_id)
            continue

        obs = WindowObservation(
            window_index=window_index,
            video_time_s=window.start_s,
            entities=[(e.stable_id, e.label, e.kind, e.confidence) for e in raw.entities],
            relations=[(r.subject_id, r.verb, r.target_id) for r in raw.relations],
            activity_description=raw.activity_description,
            phase_id=str(phase_hint) if phase_hint is not None else None,
        )

        decision = pipeline.process(obs)
        await write_audit_record(case_id, obs, raw, decision.suppressed)
        vitals = sample_at(window.start_s, case_duration_s)
        try:
            written = await _emit(case_id, pipeline, decision, obs, vitals, spine)
        except Exception:
            # Same rule as the Gemini failure above: a window that cannot be
            # written is a lost window, not a lost case. The pipeline's own
            # state has already advanced, so the next window diffs correctly
            # against this one.
            logger.exception("perception[%s]: emit failed for window %d, continuing", case_id, window_index)
            continue

        if written == 0 and pipeline.heartbeat_due(obs.video_time_s):
            # Steady state has run long enough to be worth proving liveness —
            # one lightweight event per interval, not a per-window "still going".
            await apply_state_patches(case_id, event_patches(pipeline.build_heartbeat(obs), pipeline.next_seq(), obs, _SOURCE_TOOL))
            written = 1

        logger.info(
            "perception[%s]: window %d t=%.0fs -> %d event(s)%s",
            case_id,
            window_index,
            window.start_s,
            written,
            f", suppressed: {'; '.join(decision.suppressed)}" if decision.suppressed else "",
        )

    return pipeline
