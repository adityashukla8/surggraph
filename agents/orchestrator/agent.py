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

Today, Monitor is the only real agent that exists — `sub_agents` wraps its
real ADK coordinator so Registry/Observability tooling sees a genuine
child, structured to extend as Scene Graph Builder/Anticipation/etc. get
built (append to the list, not a rewrite).

The heavy lifting lives in the plain async open_case() function, mirroring
the pattern already established for Monitor (agents/monitor/agent.py's
monitor_case is "what Orchestrator calls" — this module is what actually
calls it): OrchestratorAgent._run_async_impl is a thin ADK-flow adapter,
not where the real logic lives — matching MonitorCoordinatorAgent's own
docstring rationale for the same split.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from agents.monitor.agent import monitor_case
from agents.monitor.coordinator import MonitorCoordinatorAgent
from state.schema import DivergenceEvent, GraphNodePatch
from tools.state_tools import apply_state_patch

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
DEMO_BEAT_PATH = DATA_ROOT / "validation" / "monitor_demo_beat.json"

# Used ONLY when no real evidenced demo-beat file exists yet for the
# requested video (scripts/run_monitor_validation_sweep.py hasn't been run
# against it) — loudly logged, never silently presented as equivalent to
# a real evidenced true-positive window.
_PLACEHOLDER_START_S = 0.0
_PLACEHOLDER_END_S = 30.0


def _pick_monitor_window(video_id: str) -> tuple[float, float]:
    """Real evidenced window from the offline validation sweep
    (scripts/run_monitor_validation_sweep.py's best-true-positive-margin
    selection, plan §3.5's demo-reliability strategy) if one exists for
    this exact video_id; otherwise a disclosed placeholder."""
    if DEMO_BEAT_PATH.exists():
        beat = json.loads(DEMO_BEAT_PATH.read_text())
        if beat.get("video_id") == video_id:
            return float(beat["start_s"]), float(beat["end_s"])
        logger.warning(
            "monitor_demo_beat.json exists but is for video_id=%r, not the requested %r — falling back to placeholder window",
            beat.get("video_id"),
            video_id,
        )
    else:
        logger.warning(
            "no evidenced demo-beat window for video_id=%r (run scripts/run_monitor_validation_sweep.py first) — "
            "using placeholder [%.1f, %.1f)s, not a real evidenced true-positive window",
            video_id,
            _PLACEHOLDER_START_S,
            _PLACEHOLDER_END_S,
        )
    return _PLACEHOLDER_START_S, _PLACEHOLDER_END_S


async def open_case(
    case_id: str, video_id: str, start_s: float | None = None, end_s: float | None = None
) -> list[DivergenceEvent]:
    """Orchestrator's actual work for opening a case: emits a real
    "workflow triggered" node (the honest marker of the moment the user
    pressed play — not fabricated, not a fake progress indicator), then
    runs Monitor's live 2-pass detection over a real window of `video_id`.
    `start_s`/`end_s` default to the real evidenced demo-beat window when
    not given explicitly."""
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=f"event:workflow-triggered-{case_id}",
            node_type="event",
            label="Autonomous workflow triggered",
            attrs={"video_id": video_id},
            source_agent="orchestrator",
            source_tool="open_case",
        ),
        reason="Case opened — user pressed play, autonomous pipeline started",
        source_agent="orchestrator",
        source_tool="open_case",
    )

    if start_s is None or end_s is None:
        window_start, window_end = _pick_monitor_window(video_id)
        start_s = start_s if start_s is not None else window_start
        end_s = end_s if end_s is not None else window_end

    return await monitor_case(case_id, video_id, start_s=start_s, end_s=end_s)


class OrchestratorAgent(BaseAgent):
    """Real ADK BaseAgent — root of the agent topology. `sub_agents` is
    declared for Registry/Observability visibility, matching the pattern
    already established by MonitorCoordinatorAgent: the actual execution
    path goes through the plain async function chain (open_case ->
    monitor_case -> run_monitor_sweep -> the real sub-agents), not by
    re-invoking these declared instances through the ADK runner directly."""

    def __init__(self, name: str = "orchestrator"):
        super().__init__(name=name, sub_agents=[MonitorCoordinatorAgent()])

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
