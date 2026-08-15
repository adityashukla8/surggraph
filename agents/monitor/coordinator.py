"""Monitor Agent coordinator (plan §3.5, restructured per docs/latency_optimization.md):
orchestrates the three real sub-agents (agents/monitor/subagents.py) over a
sliding window and combines their outputs via deterministic weighted
aggregation (agents/monitor/aggregation.py) — never a 4th LLM call for the
arithmetic.

Latency restructuring, first pass (2026-08-14, full rationale in
docs/latency_optimization.md): real per-window latency (median 163s,
offline sweep) was dominated by two things — native video's real per-call
cost, and the screen→escalate→deep structure being *sequential*. Fixed by:
  - Screen tier: switched from native video to still frames (~2.9x cheaper
    per the benchmark) — it's the tier that always runs, so this is where
    cost reduction actually matters.
  - Deep tier: made UNCONDITIONAL across all 6 error categories (not gated
    on screen's escalation choice) at a FIXED "attending" tier (dropping
    Ψ-based tier routing — the plan's own cut-order already flagged this as
    ~3% accuracy impact, now cut for latency instead of schedule reasons).
    Kept native video at first — that's where its real accuracy value is.
  - Screen and deep run CONCURRENTLY (asyncio.gather), not sequentially —
    a window's wall time is max(screen, deep) instead of screen + deep.

Latency restructuring, second pass (same day, after live use showed
nothing meaningful on the graph for ~2 minutes of real video playback):
making the deep tier unconditional meant EVERY window now pays native
video's real per-call cost (dominated by GCS fetch + server-side decode,
not clip length — confirmed via the benchmark, so a shorter window alone
doesn't fix it). Deep tier switched to still frames too — same real frame
sample as the screen pass now (STILL_FRAME_PROFILE), with the two tiers
differing in REASONING framing (broad scan vs. focused re-examination),
not input modality. Window size also dropped 10s -> 5s, whose benefit is
unlocked precisely because both tiers are stills now (frame count scales
with window length; it didn't meaningfully move native video's fetch/decode-
dominated cost). Real, disclosed accuracy tradeoff: native video previously
caught a real event still-frame sampling missed — accepted per the user's
explicit latency-over-accuracy priority.
  - Net: every window still gets a real verdict on all 6 categories (more
    thorough than before, not less), at real added cost, for a real
    latency win on the dimension that mattered (wall clock, and especially
    time-to-first-meaningful-result).

`MonitorCoordinatorAgent` is a `BaseAgent` subclass, not an `LlmAgent` and
not `ParallelAgent`. Verified directly against the installed ADK 2.6.3 this
session:
  - `ParallelAgent` carries a real `@deprecated(...)` decorator ("deprecated
    in favor of Workflow and will be removed in a future version").
  - `BaseAgent`/`_run_async_impl` carry NO deprecation marker, and
    `BaseAgent.run_async` genuinely calls `self._run_async_impl(ctx)` — this
    is the live, current extension point, not a legacy-bypassed one.
  - `BaseAgent.sub_agents: list[BaseAgent]` is a real field — declaring the
    three SCREEN-mode sub-agents there gives Registry/Observability tooling
    genuine children to see. DEEP-mode sub-agents are cached separately
    (one per role×category — 18 total, fixed tier means they're stable
    across windows now) and invoked directly, same pattern, just not part
    of the fixed `sub_agents` list.

The heavy lifting lives in plain async module-level functions
(`run_monitor_window`, `run_monitor_sweep`) rather than being locked inside
`_run_async_impl` — `scripts/run_monitor_validation_sweep.py` calls
`run_monitor_sweep` directly as a library function for the full-video
offline batch (going through a full ADK InvocationContext per window would
be pure overhead for that case), while `MonitorCoordinatorAgent._run_async_impl`
is a thin, genuinely-functional adapter for when Orchestrator invokes
Monitor through the standard ADK flow during the live demo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import BaseModel

from agents.monitor.aggregation import DEFAULT_ALPHA, DEFAULT_THRESHOLD, aggregate
from agents.monitor.knowledge import ERROR_KNOWLEDGE_LIBRARY, compute_psi
from agents.monitor.subagents import STILL_FRAME_PROFILE, DeepOutput, ScreenOutput, build_subagent
from state.schema import DivergenceEvent, ErrorCategory, ExpertiseTier, MonitorSubAgentAssessment, SubAgentRole
from tools.adk_runner import run_llm_agent_once
from tools.sedmamba_labels import ErrorAnnotations, MonitorWindow, generate_windows, load_error_annotations
from tools.video_utils import DEFAULT_WINDOW_S, build_multimodal_content, find_video_path, sample_frames

_ROLES: list[SubAgentRole] = ["temporal", "spatial", "procedural"]
_CATEGORIES: list[ErrorCategory] = list(ERROR_KNOWLEDGE_LIBRARY.keys())
_FIXED_DEEP_TIER: ExpertiseTier = "attending"

# Built once — the screen-mode instruction is role-only (not window-
# specific), so every window's screen pass reuses these same three agents.
# NOT what MonitorCoordinatorAgent.sub_agents declares — an ADK agent
# instance can only ever have one parent, permanently (confirmed: a second
# MonitorCoordinatorAgent() construction raises a real pydantic
# ValidationError — "already has a parent agent" — if sub_agents reuses
# these same singletons). __init__ below builds fresh, identically-
# configured instances for that declaration instead.
_SCREEN_AGENTS = {role: build_subagent(role, mode="screen") for role in _ROLES}

# Built once too — the deep-mode instruction now only varies by role and
# category (tier is fixed at "attending"), so these 18 (3 roles x 6
# categories) are stable across every window, not rebuilt per call like
# when tier used to vary per escalation.
_DEEP_AGENTS = {
    (role, category): build_subagent(role, mode="deep", tier=_FIXED_DEEP_TIER, category=category)
    for role in _ROLES
    for category in _CATEGORIES
}


class CategoryResult(BaseModel):
    """One error category's real, independent verdict for a window — the
    unconditional-deep-pass replacement for the old single `escalated_category`
    shape. A window can have zero, one, or several of these fire true now."""

    composite_score: float
    is_divergence: bool
    assessments: list[MonitorSubAgentAssessment]


class MonitorWindowAssessment(BaseModel):
    """Internal result of running the full pipeline on one window — richer
    than the CaseState-facing DivergenceEvent(s) (only built for categories
    that actually fired; see build_divergence_events).

    `category_results` holds every category's real, independent verdict
    (new). The scalar `escalated_category`/`psi`/`tier_used`/`composite_score`
    fields are kept for schema/caller stability — they summarize the
    highest-scoring category (whichever fired with the largest margin, or
    the highest-scoring category overall if none fired) rather than
    reflecting a screen-pass escalation choice, since deep now runs
    unconditionally for all categories."""

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
    category_results: dict[str, CategoryResult]


# Caps TOTAL concurrent Gemini calls across every window/pass in a sweep.
# Retry-with-backoff on 429 is handled at the SDK level (tools/gemini_model.py's
# HttpRetryOptions, applied to every agent's client) — this semaphore is
# defense-in-depth to reduce how often Dynamic Shared Quota contention
# happens in the first place. Raised from 4 (docs/latency_optimization.md —
# cost is now explicitly acceptable in exchange for latency; real quota
# contention is confirmed to start around 3-4 concurrent calls, so this
# still relies on the SDK-level retry/backoff to absorb the overage rather
# than assuming a higher ceiling exists).
_GEMINI_CONCURRENCY = asyncio.Semaphore(12)


async def _run_agent_once(agent, content: types.Content, output_model: type[BaseModel]) -> BaseModel:
    async with _GEMINI_CONCURRENCY:
        return await run_llm_agent_once(agent, content, output_model, app_name="surggraph_monitor")


def _sample_role_frames(video_path: Path, window: MonitorWindow, role: SubAgentRole) -> list:
    profile = STILL_FRAME_PROFILE[role]
    return sample_frames(
        video_path,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        n_frames=profile["n_frames"],
        resize_to=profile["resize_to"],
    )


async def _run_screen_pass_stills(video_path: Path, window: MonitorWindow) -> dict[SubAgentRole, ScreenOutput]:
    """Cheap, fast tier — still frames, not native video (docs/latency_optimization.md:
    real ~2.9x per-call latency win, confirmed via docs/monitor_agent_video_input_benchmark.md)."""

    async def one(role: SubAgentRole) -> tuple[SubAgentRole, ScreenOutput]:
        frames = _sample_role_frames(video_path, window, role)
        content = build_multimodal_content(
            instruction_text=f"Analyze this ~{window.end_s - window.start_s:.0f}s window (video seconds {window.start_s:.1f}-{window.end_s:.1f}).",
            frames=frames,
        )
        result = await _run_agent_once(_SCREEN_AGENTS[role], content, ScreenOutput)
        return role, result

    results = await asyncio.gather(*(one(role) for role in _ROLES))
    return dict(results)


async def _run_deep_pass_all_categories(video_path: Path, window: MonitorWindow) -> dict[ErrorCategory, dict[SubAgentRole, DeepOutput]]:
    """Deep tier, UNCONDITIONAL across all 6 categories (not gated on
    screen's escalation choice) at a fixed 'attending' tier — 18 real
    parallel calls per window (3 roles x 6 categories), real added cost,
    accepted for latency. Now still frames too, not native video (second
    latency pass, docs/latency_optimization.md): once this tier became
    unconditional, its real per-window cost (dominated by native video's
    GCS-fetch + server-side-decode overhead, not proportional to clip
    length) was the actual long pole behind "nothing meaningful on the
    graph for ~2 minutes." Same real frame sample as the screen pass for
    a given window (STILL_FRAME_PROFILE) — the two tiers now differ in
    REASONING framing (broad 6-category scan vs. focused single-category,
    tier-voiced re-examination), not input modality."""

    async def one(role: SubAgentRole, category: ErrorCategory) -> tuple[ErrorCategory, SubAgentRole, DeepOutput]:
        frames = _sample_role_frames(video_path, window, role)
        content = build_multimodal_content(
            instruction_text=f"Deep review, category={category}, tier={_FIXED_DEEP_TIER} (video seconds {window.start_s:.1f}-{window.end_s:.1f}).",
            frames=frames,
        )
        result = await _run_agent_once(_DEEP_AGENTS[(role, category)], content, DeepOutput)
        return category, role, result

    flat = await asyncio.gather(*(one(role, category) for role in _ROLES for category in _CATEGORIES))
    by_category: dict[ErrorCategory, dict[SubAgentRole, DeepOutput]] = {c: {} for c in _CATEGORIES}
    for category, role, result in flat:
        by_category[category][role] = result
    return by_category


async def run_monitor_window(
    video_id: str,
    window: MonitorWindow,
    alpha_weights: dict[SubAgentRole, float] = DEFAULT_ALPHA,
    threshold: float = DEFAULT_THRESHOLD,
) -> MonitorWindowAssessment:
    video_path = find_video_path(video_id)
    if video_path is None:
        raise FileNotFoundError(f"no local source video found for {video_id!r}")

    # Screen and deep (both stills now, docs/latency_optimization.md's
    # second pass) run CONCURRENTLY, not sequentially — a window's wall
    # time is max(screen, deep) instead of screen + deep. Screen's own
    # opinions aren't consumed further here (deep is unconditional now);
    # it still runs because it's the cheap tier that populates the graph
    # fast, and agents/monitor/agent.py's real-time traceability uses it.
    _screen_results, deep_by_category = await asyncio.gather(
        _run_screen_pass_stills(video_path, window),
        _run_deep_pass_all_categories(video_path, window),
    )

    category_results: dict[str, CategoryResult] = {}
    for category in _CATEGORIES:
        role_outputs = deep_by_category[category]
        o_values = {role: role_outputs[role].error_present for role in _ROLES}
        composite_score, is_divergence = aggregate(
            o_values["temporal"], o_values["spatial"], o_values["procedural"], alpha=alpha_weights, threshold=threshold
        )
        assessments = [
            MonitorSubAgentAssessment(
                agent_role=role,
                tier_used=_FIXED_DEEP_TIER,
                error_present=role_outputs[role].error_present,
                confidence=role_outputs[role].confidence,
                reasoning=role_outputs[role].reasoning,
                frames_examined=[window.start_frame, window.end_frame],
            )
            for role in _ROLES
        ]
        category_results[category] = CategoryResult(composite_score=composite_score, is_divergence=is_divergence, assessments=assessments)

    fired = {c: r for c, r in category_results.items() if r.is_divergence}
    top_category = max(fired, key=lambda c: fired[c].composite_score) if fired else max(
        category_results, key=lambda c: category_results[c].composite_score
    )
    top_result = category_results[top_category]
    psi = compute_psi(top_category)  # informational now — no longer gates which tier runs

    return MonitorWindowAssessment(
        window_id=window.window_id,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        sub_agent_assessments=top_result.assessments,
        escalated_category=top_category,
        psi=psi,
        tier_used=_FIXED_DEEP_TIER,
        composite_score=top_result.composite_score,
        threshold_used=threshold,
        is_divergence=bool(fired),
        category_results=category_results,
    )


async def run_monitor_sweep(
    video_id: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    stride_s: float = 1.0,
    window_s: float = DEFAULT_WINDOW_S,
    max_concurrent_windows: int = 6,
    annotations: ErrorAnnotations | None = None,
    on_window_complete: Callable[[MonitorWindowAssessment], Awaitable[None]] | None = None,
) -> list[MonitorWindowAssessment]:
    """Runs the full detection pipeline over every window in [start_s, end_s).
    `annotations` is only used to derive the real sample-rate-based window
    grid (tools/sedmamba_labels.py) — never to decide any window's outcome;
    pass an already-loaded ErrorAnnotations to avoid re-reading the pickle
    on every call.

    `on_window_complete`, if given, is awaited as EACH window finishes (via
    asyncio.as_completed, not only after the whole sweep) — this is what
    lets a caller (agents/monitor/agent.py) stream every sub-agent's real
    input/output onto the graph in real time, not just the final fired
    result batched at the end. The offline validation sweep
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


def build_divergence_events(case_id: str, assessment: MonitorWindowAssessment, phase: str) -> list[DivergenceEvent]:
    """One real DivergenceEvent per category that actually fired this window
    — a window can now have zero, one, or several (unconditional deep pass
    across all 6 categories, docs/latency_optimization.md), unlike the old
    single-escalated-category design."""
    events: list[DivergenceEvent] = []
    for category, result in assessment.category_results.items():
        if not result.is_divergence:
            continue
        events.append(
            DivergenceEvent(
                case_id=case_id,
                window_id=assessment.window_id,
                frame=assessment.start_frame,
                window_start_frame=assessment.start_frame,
                window_end_frame=assessment.end_frame,
                phase=phase,
                error_category=category,
                source="monitor_agentic_detection",
                sub_agent_assessments=result.assessments,
                psi=compute_psi(category),
                tier_used=_FIXED_DEEP_TIER,
                composite_score=result.composite_score,
                threshold_used=assessment.threshold_used,
                confidence=max((a.confidence for a in result.assessments), default=0.0),
                reasoning_trace=(
                    f"composite_score={result.composite_score:.2f} vs threshold={assessment.threshold_used:.2f}; "
                    f"{sum(1 for a in result.assessments if a.error_present)}/3 agents flagged '{category}'"
                ),
                source_agent="monitor_coordinator",
                source_tool="run_monitor_window",
            )
        )
    return events


class MonitorCoordinatorAgent(BaseAgent):
    """Real ADK BaseAgent coordinator — see module docstring for why this is
    a BaseAgent (not LlmAgent, not ParallelAgent) and why the actual per-
    window logic lives in the module-level functions above rather than only
    inside _run_async_impl."""

    def __init__(self, name: str = "monitor_coordinator"):
        # Fresh, identically-configured instances — not _SCREEN_AGENTS.values()
        # (see that dict's own comment: reusing those singletons here breaks
        # the moment MonitorCoordinatorAgent is constructed a second time).
        super().__init__(name=name, sub_agents=[build_subagent(role, mode="screen") for role in _ROLES])

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        video_id = state.get("video_id")
        case_id = state.get("case_id", ctx.session.id)
        start_s = float(state.get("monitor_start_s", 0.0))
        end_s = state.get("monitor_end_s")
        end_s = float(end_s) if end_s is not None else None

        if not video_id:
            yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text="monitor_coordinator: no video_id in session state, nothing to do")]))
            return

        # Deferred import: agents/monitor/agent.py imports FROM this module
        # (MonitorWindowAssessment, build_divergence_events, run_monitor_sweep),
        # so importing monitor_case at module level here would be circular.
        # This is the one place the "proper" ADK-invocation path needs it —
        # calling run_monitor_sweep directly (as an earlier version of this
        # method did) skips agent.py's on_window_complete callback entirely,
        # meaning Orchestrator invoking Monitor through the standard ADK flow
        # would silently emit ZERO real-time graph traceability, unlike the
        # agent.py::monitor_case path used everywhere else. Unify on the one
        # entry point that actually writes to the graph.
        from agents.monitor.agent import monitor_case

        fired = await monitor_case(case_id, video_id, start_s=start_s, end_s=end_s)
        summary = f"Monitor swept case {case_id}, {len(fired)} divergence(s) fired."
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text=summary)]))
