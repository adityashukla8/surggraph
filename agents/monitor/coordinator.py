"""Monitor Agent coordinator (plan §3.5): orchestrates the three real
sub-agents (agents/monitor/subagents.py) over a sliding window and combines
their outputs via deterministic risk-routing + weighted aggregation
(agents/monitor/aggregation.py) — never a 4th LLM call for the arithmetic.

`MonitorCoordinatorAgent` is a `BaseAgent` subclass, not an `LlmAgent` and
not `ParallelAgent`. Verified directly against the installed ADK 2.6.3 this
session:
  - `ParallelAgent` carries a real `@deprecated(...)` decorator ("deprecated
    in favor of Workflow and will be removed in a future version").
  - `BaseAgent`/`_run_async_impl` carry NO deprecation marker, and
    `BaseAgent.run_async` genuinely calls `self._run_async_impl(ctx)` — this
    is the live, current extension point, not a legacy-bypassed one (the
    "Workflow Graph engine bypasses legacy overrides" warning found during
    research applies only to the separate, opt-in `Workflow`/`Node`/`Edge`
    graph primitives, not to standard `BaseAgent` subclassing).
  - `BaseAgent.sub_agents: list[BaseAgent]` is a real field — declaring the
    three SCREEN-mode sub-agents there gives Registry/Observability tooling
    genuine children to see. DEEP-mode sub-agents are built fresh per
    escalation (their prompt depends on the escalated category + tier, so a
    fixed instance doesn't make sense) and invoked directly, same pattern,
    just not part of the fixed `sub_agents` list.

The heavy lifting lives in plain async module-level functions
(`run_monitor_window`, `run_monitor_sweep`) rather than being locked inside
`_run_async_impl` — `scripts/run_monitor_validation_sweep.py` calls
`run_monitor_sweep` directly as a library function for a 262-window offline
batch (going through a full ADK InvocationContext per window would be pure
overhead for that case), while `MonitorCoordinatorAgent._run_async_impl`
is a thin, genuinely-functional adapter for when Orchestrator invokes
Monitor through the standard ADK flow during the live demo.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Awaitable, Callable

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

from agents.monitor.aggregation import DEFAULT_ALPHA, DEFAULT_THRESHOLD, aggregate, pick_escalation_candidate
from agents.monitor.knowledge import compute_psi, route_tier
from agents.monitor.subagents import (
    VIDEO_FPS_PROFILE,
    DeepOutput,
    ScreenOutput,
    build_subagent,
)
from state.schema import DivergenceEvent, ErrorCategory, ExpertiseTier, MonitorSubAgentAssessment, SubAgentRole
from tools.sedmamba_labels import ErrorAnnotations, MonitorWindow, generate_windows, load_error_annotations
from tools.video_utils import build_video_window_content, find_video_gcs_uri, video_mime_type

_ROLES: list[SubAgentRole] = ["temporal", "spatial", "procedural"]

# Built once — the screen-mode instruction is role-only (not window/category
# specific), so every window's screen pass reuses these same three agents.
# These are also what MonitorCoordinatorAgent.sub_agents declares.
_SCREEN_AGENTS = {role: build_subagent(role, mode="screen") for role in _ROLES}


class MonitorWindowAssessment(BaseModel):
    """Internal result of running the full 2-pass pipeline on one window —
    richer than the CaseState-facing DivergenceEvent (which is only built
    for windows that actually fire; see build_divergence_event)."""

    window_id: str
    start_frame: int
    end_frame: int
    sub_agent_assessments: list[MonitorSubAgentAssessment]
    escalated_category: ErrorCategory | None
    psi: int | None
    tier_used: ExpertiseTier | None
    composite_score: float
    threshold_used: float
    is_divergence: bool


# Caps TOTAL concurrent Gemini calls across every window/pass in a sweep.
# Retry-with-backoff on 429 is handled at the SDK level (tools/gemini_model.py's
# HttpRetryOptions, applied to every agent's client) — this semaphore is
# defense-in-depth to reduce how often Dynamic Shared Quota contention
# happens in the first place, since per-window concurrency (3 agents) times
# per-sweep window concurrency (max_concurrent_windows) otherwise has no
# combined bound.
_GEMINI_CONCURRENCY = asyncio.Semaphore(4)


async def _run_agent_once(agent, content: types.Content, output_model: type[BaseModel]) -> BaseModel:
    async with _GEMINI_CONCURRENCY:
        runner = InMemoryRunner(agent=agent, app_name="surggraph_monitor")
        session = await runner.session_service.create_session(app_name="surggraph_monitor", user_id="monitor")
        final_text: str | None = None
        async for event in runner.run_async(user_id="monitor", session_id=session.id, new_message=content):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        final_text = part.text
    if final_text is None:
        raise RuntimeError(f"{agent.name} produced no text output")
    return output_model.model_validate_json(final_text)


async def _run_screen_pass(gcs_uri: str, mime_type: str, window: MonitorWindow) -> dict[SubAgentRole, ScreenOutput]:
    async def one(role: SubAgentRole) -> tuple[SubAgentRole, ScreenOutput]:
        content = build_video_window_content(
            gcs_uri,
            mime_type,
            start_s=window.start_s,
            end_s=window.end_s,
            fps=VIDEO_FPS_PROFILE[role],
            instruction_text=f"Analyze this ~{window.end_s - window.start_s:.0f}s window (video seconds {window.start_s:.1f}-{window.end_s:.1f}).",
        )
        result = await _run_agent_once(_SCREEN_AGENTS[role], content, ScreenOutput)
        return role, result

    results = await asyncio.gather(*(one(role) for role in _ROLES))
    return dict(results)


async def _run_deep_pass(
    gcs_uri: str, mime_type: str, window: MonitorWindow, category: ErrorCategory, tier: ExpertiseTier
) -> dict[SubAgentRole, DeepOutput]:
    async def one(role: SubAgentRole) -> tuple[SubAgentRole, DeepOutput]:
        content = build_video_window_content(
            gcs_uri,
            mime_type,
            start_s=window.start_s,
            end_s=window.end_s,
            fps=VIDEO_FPS_PROFILE[role],
            instruction_text=f"Deep review, category={category}, tier={tier} (video seconds {window.start_s:.1f}-{window.end_s:.1f}).",
        )
        agent = build_subagent(role, mode="deep", tier=tier, category=category)
        result = await _run_agent_once(agent, content, DeepOutput)
        return role, result

    results = await asyncio.gather(*(one(role) for role in _ROLES))
    return dict(results)


async def run_monitor_window(
    video_id: str,
    window: MonitorWindow,
    alpha_weights: dict[SubAgentRole, float] = DEFAULT_ALPHA,
    threshold: float = DEFAULT_THRESHOLD,
) -> MonitorWindowAssessment:
    gcs_uri = find_video_gcs_uri(video_id)
    if gcs_uri is None:
        raise FileNotFoundError(f"no video found in GCS for {video_id} — upload it first (see docs/monitor_agent_video_input_benchmark.md)")
    mime_type = video_mime_type(gcs_uri)

    screen_results = await _run_screen_pass(gcs_uri, mime_type, window)

    category_confidences = [
        {op.category: op.confidence for op in screen.opinions if op.suspected} for screen in screen_results.values()
    ]
    escalated_category = pick_escalation_candidate(category_confidences)

    assessments: list[MonitorSubAgentAssessment] = []

    if escalated_category is not None:
        psi = compute_psi(escalated_category)
        tier = route_tier(psi)
        deep_results = await _run_deep_pass(gcs_uri, mime_type, window, escalated_category, tier)
        o_values = {role: deep_results[role].error_present for role in _ROLES}
        for role in _ROLES:
            deep = deep_results[role]
            assessments.append(
                MonitorSubAgentAssessment(
                    agent_role=role,
                    tier_used=tier,
                    error_present=deep.error_present,
                    confidence=deep.confidence,
                    reasoning=deep.reasoning,
                    frames_examined=[window.start_frame, window.end_frame],
                )
            )
    else:
        psi = None
        tier = None
        # No category cleared the escalation bar — the screen-pass `suspected`
        # booleans stand in as the final O values (disclosed via psi/tier_used
        # being None, per plan §3.5).
        o_values = {}
        for role in _ROLES:
            screen = screen_results[role]
            suspected_any = any(op.suspected for op in screen.opinions)
            top_conf = max((op.confidence for op in screen.opinions if op.suspected), default=0.0)
            o_values[role] = suspected_any
            top_obs = next((op.observation for op in screen.opinions if op.suspected), "no category suspected")
            assessments.append(
                MonitorSubAgentAssessment(
                    agent_role=role,
                    tier_used="resident",  # screen pass has no tier framing; resident is the conservative default label
                    error_present=suspected_any,
                    confidence=top_conf,
                    reasoning=top_obs,
                    frames_examined=[window.start_frame, window.end_frame],
                )
            )

    composite_score, is_divergence = aggregate(
        o_values["temporal"], o_values["spatial"], o_values["procedural"], alpha=alpha_weights, threshold=threshold
    )

    return MonitorWindowAssessment(
        window_id=window.window_id,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        sub_agent_assessments=assessments,
        escalated_category=escalated_category,
        psi=psi,
        tier_used=tier,
        composite_score=composite_score,
        threshold_used=threshold,
        is_divergence=is_divergence,
    )


async def run_monitor_sweep(
    video_id: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    stride_s: float = 1.0,
    window_s: float = 10.0,
    max_concurrent_windows: int = 6,
    annotations: ErrorAnnotations | None = None,
    on_window_complete: Callable[[MonitorWindowAssessment], Awaitable[None]] | None = None,
) -> list[MonitorWindowAssessment]:
    """Runs the full 2-pass detection pipeline over every window in
    [start_s, end_s). `annotations` is only used to derive the real
    sample-rate-based window grid (tools/sedmamba_labels.py) — never to
    decide any window's outcome; pass an already-loaded ErrorAnnotations to
    avoid re-reading the pickle on every call.

    `on_window_complete`, if given, is awaited as EACH window finishes
    (via asyncio.as_completed, not only after the whole sweep) — this is
    what lets a caller (agents/monitor/agent.py) stream every sub-agent's
    real input/output onto the graph in real time, not just the final
    fired result batched at the end. The offline validation sweep
    (scripts/run_monitor_validation_sweep.py) doesn't pass this — it never
    touches the graph."""
    ann = annotations or load_error_annotations(video_id)
    windows = generate_windows(ann, window_s=window_s, stride_s=stride_s, start_s=start_s, end_s=end_s)

    semaphore = asyncio.Semaphore(max_concurrent_windows)

    async def bounded(window: MonitorWindow) -> MonitorWindowAssessment:
        async with semaphore:
            return await run_monitor_window(video_id, window)

    tasks = [asyncio.ensure_future(bounded(w)) for w in windows]
    results: list[MonitorWindowAssessment] = []
    for coro in asyncio.as_completed(tasks):
        assessment = await coro
        results.append(assessment)
        if on_window_complete is not None:
            await on_window_complete(assessment)
    return results


def build_divergence_event(case_id: str, assessment: MonitorWindowAssessment, phase: str) -> DivergenceEvent:
    return DivergenceEvent(
        case_id=case_id,
        window_id=assessment.window_id,
        frame=assessment.start_frame,
        window_start_frame=assessment.start_frame,
        window_end_frame=assessment.end_frame,
        phase=phase,
        error_category=assessment.escalated_category or "manual_injection",
        source="monitor_agentic_detection",
        sub_agent_assessments=assessment.sub_agent_assessments,
        psi=assessment.psi,
        tier_used=assessment.tier_used,
        composite_score=assessment.composite_score,
        threshold_used=assessment.threshold_used,
        confidence=max((a.confidence for a in assessment.sub_agent_assessments), default=0.0),
        reasoning_trace=(
            f"composite_score={assessment.composite_score:.2f} vs threshold={assessment.threshold_used:.2f}; "
            f"{sum(1 for a in assessment.sub_agent_assessments if a.error_present)}/3 agents flagged "
            f"'{assessment.escalated_category}'"
        ),
        source_agent="monitor_coordinator",
        source_tool="run_monitor_window",
    )


class MonitorCoordinatorAgent(BaseAgent):
    """Real ADK BaseAgent coordinator — see module docstring for why this is
    a BaseAgent (not LlmAgent, not ParallelAgent) and why the actual per-
    window logic lives in the module-level functions above rather than only
    inside _run_async_impl."""

    def __init__(self, name: str = "monitor_coordinator"):
        super().__init__(name=name, sub_agents=list(_SCREEN_AGENTS.values()))

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        video_id = state.get("video_id")
        case_id = state.get("case_id", ctx.session.id)
        phase = state.get("phase", "unknown")
        start_s = float(state.get("monitor_start_s", 0.0))
        end_s = state.get("monitor_end_s")
        end_s = float(end_s) if end_s is not None else None

        if not video_id:
            yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text="monitor_coordinator: no video_id in session state, nothing to do")]))
            return

        assessments = await run_monitor_sweep(video_id, start_s=start_s, end_s=end_s)
        fired = [a for a in assessments if a.is_divergence]
        summary = f"Monitor swept {len(assessments)} window(s), {len(fired)} divergence(s) fired."
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text=summary)]))
