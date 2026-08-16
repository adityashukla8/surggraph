"""Orchestrator — the root ADK agent (plan §5.2 item 1: "receives video,
opens the case, dispatches sub-agents, owns the state machine").

The trigger is a single HTTP call (services/orchestrator_service's POST
/cases/open), fired by the frontend on the FIRST time the user presses play
on the video — not page load, not every subsequent play/pause toggle (plan
§11, revised after discussion: page load alone doesn't represent the user
actually starting the workflow). Every call mints a brand-new case_id — no
get-or-create, no shared state between concurrent users — so this module's
job is "given a fresh case_id and a video_id, run the real pipeline."

Once triggered, the pipeline runs independently of the video's own
play/pause state — there is no artificial gating tying agent computation to
video playback position. Real Gemini analysis latency (confirmed this
session: ~163s median per 10s-of-video window) is far slower than the
video's own real-time duration, so any strict "agents never compute ahead
of playback position" model would force the video itself to play in bursts
disconnected from smooth playback — rejected as incompatible with a
coherent live demo. Agents simply compute as fast as they genuinely can and
write real results to the graph as they land; the video plays normally.

Today, Error Detection is the only real agent that exists — `sub_agents` wraps its
real ADK coordinator so Registry/Observability tooling sees a genuine
child, structured to extend as further agents get
built (append to the list, not a rewrite).

The heavy lifting lives in the plain async open_case() function, mirroring
the pattern already established for Error Detection (agents/error_detection/agent.py's
error_detection_case is "what Orchestrator calls" — this module is what actually
calls it): OrchestratorAgent._run_async_impl is a thin ADK-flow adapter,
not where the real logic lives — matching ErrorDetectionCoordinatorAgent's own
docstring rationale for the same split.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from agents.error_detection.agent import SUB_AGENT_LABELS, error_detection_case
from agents.error_detection.coordinator import ErrorDetectionCoordinatorAgent
from agents.perception.agent import perception_case
from agents.perception.subagent import build_subagent as build_perception_subagent
from state import node_ids
from state.schema import DivergenceEvent, GraphEdgePatch, GraphNodePatch
from tools.patient_twin import load_patient_twin, summarize_for_prompt as summarize_patient_twin
from tools.state_tools import apply_state_patch, apply_state_patches
from tools.video_utils import find_video_duration_s, format_video_time_range

# Real, fixed pipeline structure (not decorative) — Orchestrator dispatches
# exactly these three agents; Error Detection Coordinator owns exactly these three
# sub-agents (its real ADK sub_agents, agents/error_detection/coordinator.py).
# Labels here must match each agent's own internal registration exactly
# (agents/scene_graph_builder/agent.py::_ensure_agent_node,
# each agent's own _ensure_agent_node) — both write the same
# node_id, so a mismatch would just show as a harmless but confusing label
# flicker once the redundant internal call lands.
# Anticipation is deliberately NOT dispatched: docs/agentic_workflow.md's
# roster has no card for it. Its code still exists under agents/anticipation/
# and is not deleted — two places will need revisiting if it returns, namely
# that doc's "three long-running sweeps" line and plan_v2 §6 step 11's
# anticipation next-phase accuracy in the Benchmark Agent.
_TOP_LEVEL_AGENTS = (
    ("agent:error_detection_coordinator", "error_detection_coordinator", "Error Detection Coordinator"),
    ("agent:perception", "perception", "Perception Agent"),
    # Event-driven, not swept: these do not run on a cadence, they wake when a
    # qualifying node lands on the graph. They are still drawn in the static
    # skeleton so their output has somewhere to hang from the moment it exists
    # — an agent node created lazily on first use leaves everything it produced
    # unreachable from the trigger until then.
    ("agent:complication_reasoning", "complication_reasoning", "Complication Reasoning"),
    ("agent:literature_retrieval", "literature_retrieval", "Literature Retrieval"),
)

logger = logging.getLogger(__name__)


async def _draw_static_hierarchy(case_id: str, start_s: float, end_s: float, video_id: str) -> None:
    """Writes the ENTIRE static pipeline skeleton — trigger node, all 3
    top-level agent nodes, Error Detection's 3 sub-agent nodes, and every hierarchy
    edge between them — sequentially, right here, before any agent sweep
    (and therefore any real Gemini call) has been kicked off.

    Real bug this fixes: each agent's own internal registration call
    (agents/error_detection/agent.py::_ensure_agent_nodes, scene_graph_builder's/
    each agent's own _ensure_agent_node) IS correct and IS the first line
    of its own sweep — but once dozens of concurrent Gemini calls are in
    flight, those tiny, cheap registration writes (dispatched via
    asyncio.to_thread, sharing Python's default thread pool with whatever
    the Gemini SDK's own blocking I/O uses) can get starved behind
    minutes of long-running Gemini calls queued ahead of them. Confirmed
    directly from real Firestore timestamps this session: an "agent:
    error_detection_temporal -> " hierarchy edge landed nearly 3 minutes after the
    case opened, even though the code that writes it is the literal first
    line of error_detection_case(). Drawing the static skeleton HERE, before
    asyncio.gather kicks off any sweep, means it's written while the
    thread pool is still empty — fast and reliable regardless of how busy
    the pool gets once the real per-window work starts.

    Each agent's own internal registration call still runs too (harmless,
    idempotent re-writes of the same content) — this function doesn't
    replace that, it just guarantees the skeleton doesn't have to wait for
    it."""
    trigger_node_id = node_ids.trigger(case_id)
    patient_twin_node_id = node_ids.patient_twin()
    profile = load_patient_twin()

    patches: list[tuple] = [
        (
            GraphNodePatch(
                node_id=trigger_node_id,
                node_type="trigger",
                label=f"Autonomous workflow triggered ({format_video_time_range(start_s, end_s)})",
                attrs={"video_id": video_id},
                source_agent="orchestrator",
                source_tool="open_case",
            ),
            None,
            "Case opened — user pressed play, autonomous pipeline started",
        ),
        # The patient twin is part of the static skeleton (docs/plan_v2 §6
        # step 1): complication reasoning needs it in its context slice the
        # moment the first error fires, and by then the thread pool is
        # saturated with Gemini calls.
        (
            GraphNodePatch(
                node_id=patient_twin_node_id,
                node_type="patient_twin",
                label=f"{profile['display_name']} (synthetic)",
                attrs={
                    "synthetic": True,
                    "disclosure": profile["_disclosure"],
                    "profile": profile,
                    "prompt_summary": summarize_patient_twin(profile),
                },
                source_agent="orchestrator",
                source_tool="open_case",
            ),
            None,
            "Synthetic patient profile loaded for this case",
        ),
        (
            None,
            GraphEdgePatch(
                edge_id=node_ids.edge(trigger_node_id, patient_twin_node_id, "hierarchy"),
                source_node_id=trigger_node_id,
                target_node_id=patient_twin_node_id,
                edge_kind="hierarchy",
                source_agent="orchestrator",
                source_tool="open_case",
                reason="Synthetic patient profile loaded for this case",
            ),
            "Synthetic patient profile loaded for this case",
        ),
    ]

    for target_agent_node_id, target_agent_name, target_agent_label in _TOP_LEVEL_AGENTS:
        patches.append(
            (
                GraphNodePatch(
                    node_id=target_agent_node_id,
                    node_type="agent",
                    label=target_agent_label,
                    source_agent=target_agent_name,
                    source_tool="open_case",
                ),
                None,
                f"{target_agent_name} registered for this case",
            )
        )
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(trigger_node_id, target_agent_node_id, "hierarchy"),
                    source_node_id=trigger_node_id,
                    target_node_id=target_agent_node_id,
                    edge_kind="hierarchy",
                    source_agent="orchestrator",
                    source_tool="open_case",
                    reason=f"Orchestrator dispatched {target_agent_name}",
                ),
                f"Orchestrator dispatched {target_agent_name}",
            )
        )

    coordinator_node_id = node_ids.agent("error_detection_coordinator")
    for sub_node_id, sub_label in SUB_AGENT_LABELS.items():
        if sub_node_id == "error_detection_coordinator":
            continue
        sub_agent_node_id = node_ids.agent(sub_node_id)
        patches.append(
            (
                GraphNodePatch(
                    node_id=sub_agent_node_id,
                    node_type="agent",
                    label=sub_label,
                    source_agent=sub_node_id,
                    source_tool="open_case",
                ),
                None,
                f"{sub_label} registered for this case",
            )
        )
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(coordinator_node_id, sub_agent_node_id, "hierarchy"),
                    source_node_id=coordinator_node_id,
                    target_node_id=sub_agent_node_id,
                    edge_kind="hierarchy",
                    source_agent="error_detection_coordinator",
                    source_tool="open_case",
                    reason=f"Error Detection Coordinator owns {sub_label}",
                ),
                f"Error Detection Coordinator owns {sub_label}",
            )
        )

    # ONE batched write for the whole skeleton. Previously these went out
    # individually, and since writes to a case serialize at roughly a second
    # each, the skeleton trickled onto the screen over ~15 seconds — the very
    # first thing a viewer sees, arriving slowly. Nodes precede the edges that
    # reference them in the list, and the store assigns consecutive seqs in
    # list order, so no edge can land before its endpoints.
    await apply_state_patches(case_id, patches)


async def open_case(
    case_id: str, video_id: str, start_s: float | None = None, end_s: float | None = None
) -> list[DivergenceEvent]:
    """Orchestrator's actual work for opening a case: emits a real
    "workflow triggered" node (the honest marker of the moment the user
    pressed play — not fabricated, not a fake progress indicator), then
    runs Error Detection's live detection and Perception's live
    entity/relation/activity sweep over `video_id`, concurrently. `start_s`/`end_s`
    default to the video's own real full duration
    (docs/latency_optimization.md: "entire video's inference", not one
    bounded demo window) — `find_video_duration_s` reads this from the
    actual source file, never assumed.

    Concurrent, not sequential: real measured latency makes running
    independent agents back-to-back a real, unjustified multiplication of
    demo wait time — they write independent nodes/edges to the same graph."""
    if start_s is None:
        start_s = 0.0
    if end_s is None:
        end_s = find_video_duration_s(video_id)
        if end_s is None:
            raise ValueError(f"no source video found for {video_id!r} — cannot derive real duration to sweep")

    await _draw_static_hierarchy(case_id, start_s, end_s, video_id)

    divergences, _perception_state = await asyncio.gather(
        error_detection_case(case_id, video_id, start_s=start_s, end_s=end_s),
        perception_case(case_id, video_id, start_s=start_s, end_s=end_s),
    )
    return divergences


class OrchestratorAgent(BaseAgent):
    """Real ADK BaseAgent — root of the agent topology. `sub_agents` is
    declared for Registry/Observability visibility, matching the pattern
    already established by ErrorDetectionCoordinatorAgent: the actual execution
    path goes through the plain async function chain (open_case ->
    error_detection_case -> run_error_detection_sweep -> the real sub-agents), not by
    re-invoking these declared instances through the ADK runner directly."""

    def __init__(self, name: str = "orchestrator"):
        # Fresh instances for all three — an ADK agent can only ever have
        # one parent, permanently (confirmed: reusing a shared singleton
        # like scene_graph_builder.agent.AGENT here raises a real pydantic
        # ValidationError the moment OrchestratorAgent is constructed a
        # second time — the same bug this fixed in ErrorDetectionCoordinatorAgent).
        # Each build_subagent() call here is identically configured to its
        # module's own AGENT singleton, just not the literal same object —
        # fine for Registry visibility, which cares about real
        # configuration, not Python object identity.
        super().__init__(
            name=name,
            sub_agents=[ErrorDetectionCoordinatorAgent(), build_perception_subagent()],
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        video_id = state.get("video_id")
        case_id = state.get("case_id", ctx.session.id)

        if not video_id:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part.from_text(text="orchestrator: no video_id in session state, nothing to do")]),
            )
            return

        fired = await open_case(case_id, video_id)
        summary = f"Orchestrator opened case {case_id} for {video_id}, {len(fired)} divergence(s) fired."
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text=summary)]))
