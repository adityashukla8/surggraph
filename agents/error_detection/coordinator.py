"""Error Detection Agent coordinator (plan §3.5, restructured per docs/latency_optimization.md):
orchestrates the three real sub-agents (agents/error_detection/subagents.py) over a
sliding window and combines their outputs via deterministic weighted
aggregation (agents/error_detection/aggregation.py) — never a 4th LLM call for the
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

`ErrorDetectionCoordinatorAgent` is a `BaseAgent` subclass, not an `LlmAgent` and
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
(`run_error_detection_window`, `run_error_detection_sweep`) rather than being locked inside
`_run_async_impl` — `scripts/run_monitor_validation_sweep.py` calls
`run_error_detection_sweep` directly as a library function for the full-video
offline batch (going through a full ADK InvocationContext per window would
be pure overhead for that case), while `ErrorDetectionCoordinatorAgent._run_async_impl`
is a thin, genuinely-functional adapter for when Orchestrator invokes
Error Detection through the standard ADK flow during the live demo.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Literal

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import BaseModel

from agents.error_detection.aggregation import DEFAULT_ALPHA, DEFAULT_THRESHOLD, aggregate, pick_escalation_candidate
from agents.error_detection.knowledge import ERROR_KNOWLEDGE_LIBRARY, compute_psi
from agents.error_detection.subagents import STILL_FRAME_PROFILE, CategoryOpinion, DeepOutput, ScreenOutput, build_subagent
from state.schema import DivergenceEvent, ErrorCategory, ExpertiseTier, ErrorDetectionSubAgentAssessment, SubAgentRole
from tools.adk_runner import run_llm_agent_once
from tools.sedmamba_labels import ErrorAnnotations, ErrorDetectionWindow, generate_windows, load_error_annotations
from tools.video_utils import DEFAULT_WINDOW_S, build_multimodal_content, find_video_path, sample_frames

_ROLES: list[SubAgentRole] = ["temporal", "spatial", "procedural"]
_CATEGORIES: list[ErrorCategory] = list(ERROR_KNOWLEDGE_LIBRARY.keys())
_FIXED_DEEP_TIER: ExpertiseTier = "attending"

# Built once — the screen-mode instruction is role-only (not window-
# specific), so every window's screen pass reuses these same three agents.
# NOT what ErrorDetectionCoordinatorAgent.sub_agents declares — an ADK agent
# instance can only ever have one parent, permanently (confirmed: a second
# ErrorDetectionCoordinatorAgent() construction raises a real pydantic
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


logger = logging.getLogger(__name__)


class CategoryResult(BaseModel):
    """One error category's verdict for a window.

    Aug 18 (docs/latency_optimization.md, Fourth pass, Priority 2): reverted
    the Aug 14 unconditional-deep-pass design — the third-pass profile showed
    it costing 1203s of 2061s total Gemini time (58%), the single largest
    line item in the system, for 18 deep calls/window when only ~1 category
    typically had anything worth a closer look. Escalation is restored via
    `pick_escalation_candidate` (agents/error_detection/aggregation.py — it
    already existed, fully tested, simply unwired since the unconditional
    restructuring), which was ALSO the pre-restructuring design's own
    behavior when nothing escalated: "the screen-pass booleans stand in as
    the final O values" (that exact language is original to
    pick_escalation_candidate's docstring, predating this change).

    `reviewed` distinguishes the two real cases so nothing downstream can
    mistake a cheap screen-only opinion for the focused deep-tier review it
    is not: "deep" means the escalated category got its own 3-role,
    tier-framed deep pass (real 2-of-3 aggregation as before); "screen" means
    this category's verdict is the screen pass's own per-role suspected/
    confidence, aggregated the same way, but never independently
    re-examined. `ErrorDetectionSubAgentAssessment.tier_used` is a closed
    3-value enum (resident/attending/expert) with no slot for "screen
    only" — kept at the fixed deep tier for schema/frontend compatibility
    (nothing currently reads tier_used downstream — verified via repo-wide
    grep) rather than widening state/schema.py and ui/frontend/src/graph/
    types.ts for a field this change's scope doesn't otherwise touch; the
    disclosure lives in `reviewed` and in a `[screen-pass]` prefix on the
    fallback reasoning text instead."""

    composite_score: float
    is_divergence: bool
    assessments: list[ErrorDetectionSubAgentAssessment]
    reviewed: Literal["deep", "screen"]


class ErrorDetectionWindowAssessment(BaseModel):
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
    sub_agent_assessments: list[ErrorDetectionSubAgentAssessment]
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
        return await run_llm_agent_once(agent, content, output_model, app_name="surggraph_error_detection")


def _sample_role_frames(video_path: Path, window: ErrorDetectionWindow, role: SubAgentRole) -> list:
    profile = STILL_FRAME_PROFILE[role]
    return sample_frames(
        video_path,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        n_frames=profile["n_frames"],
        resize_to=profile["resize_to"],
    )


async def _run_screen_pass_stills(video_path: Path, window: ErrorDetectionWindow) -> dict[SubAgentRole, ScreenOutput]:
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


async def _run_deep_pass_one_category(video_path: Path, window: ErrorDetectionWindow, category: ErrorCategory) -> dict[SubAgentRole, DeepOutput]:
    """Deep tier, restricted to the ONE category the screen pass escalated
    (docs/latency_optimization.md, Fourth pass, Priority 2 — reverts the
    Aug 14 unconditional-all-6 design, which the third-pass profile found
    costing 58% of total Gemini time system-wide). 3 real parallel calls per
    window now, not 18. Still frames, same STILL_FRAME_PROFILE as the screen
    pass and the same tier-voiced re-examination framing as before — only
    the fan-out width changed, not the reasoning approach for whichever
    category actually gets reviewed."""

    async def one(role: SubAgentRole) -> tuple[SubAgentRole, DeepOutput]:
        frames = _sample_role_frames(video_path, window, role)
        content = build_multimodal_content(
            instruction_text=f"Deep review, category={category}, tier={_FIXED_DEEP_TIER} (video seconds {window.start_s:.1f}-{window.end_s:.1f}).",
            frames=frames,
        )
        result = await _run_agent_once(_DEEP_AGENTS[(role, category)], content, DeepOutput)
        return role, result

    results = await asyncio.gather(*(one(role) for role in _ROLES))
    return dict(results)


async def run_error_detection_window(
    video_id: str,
    window: ErrorDetectionWindow,
    alpha_weights: dict[SubAgentRole, float] = DEFAULT_ALPHA,
    threshold: float = DEFAULT_THRESHOLD,
) -> ErrorDetectionWindowAssessment:
    video_path = find_video_path(video_id)
    if video_path is None:
        raise FileNotFoundError(f"no local source video found for {video_id!r}")

    # Screen ALWAYS runs first now — deep depends on its escalation choice,
    # so this is genuinely sequential (screen, then deep), not the previous
    # concurrent max(screen, deep). Real, disclosed cost of restoring
    # escalation: this reintroduces the additive screen+deep wall time the
    # Aug 14 "second pass" moved away from — but deep is now 3 calls instead
    # of 18, so total call volume and total Gemini time both still drop; see
    # docs/latency_optimization.md Fourth pass for the measured net effect.
    screen_results = await _run_screen_pass_stills(video_path, window)

    # One dict per role — {category: confidence}, from every opinion that
    # role actually formed (a role omitting a category is a real absence of
    # signal, not a 0.0 confidence, so it is simply not a key here).
    category_confidences = [
        {op.category: op.confidence for op in screen_results[role].opinions} for role in _ROLES
    ]
    escalated_category = pick_escalation_candidate(category_confidences)

    deep_results: dict[SubAgentRole, DeepOutput] | None = None
    if escalated_category is not None:
        deep_results = await _run_deep_pass_one_category(video_path, window, escalated_category)

    def _screen_opinion(role: SubAgentRole, category: ErrorCategory) -> CategoryOpinion | None:
        return next((op for op in screen_results[role].opinions if op.category == category), None)

    category_results: dict[str, CategoryResult] = {}
    for category in _CATEGORIES:
        if category == escalated_category and deep_results is not None:
            # The real, focused deep-tier review — unchanged from before.
            o_values = {role: deep_results[role].error_present for role in _ROLES}
            composite_score, is_divergence = aggregate(
                o_values["temporal"], o_values["spatial"], o_values["procedural"], alpha=alpha_weights, threshold=threshold
            )
            assessments = [
                ErrorDetectionSubAgentAssessment(
                    agent_role=role,
                    tier_used=_FIXED_DEEP_TIER,
                    error_present=deep_results[role].error_present,
                    confidence=deep_results[role].confidence,
                    reasoning=deep_results[role].reasoning,
                    frames_examined=[window.start_frame, window.end_frame],
                )
                for role in _ROLES
            ]
            category_results[category] = CategoryResult(
                composite_score=composite_score, is_divergence=is_divergence, assessments=assessments, reviewed="deep"
            )
        else:
            # Not escalated this window — the screen pass's own per-role
            # suspected/confidence stands in as the real verdict, aggregated
            # through the identical weighted formula. This is the
            # pre-restructuring design's own documented fallback (see
            # pick_escalation_candidate's docstring), not a new invention.
            # A role that formed no opinion on this category contributes
            # suspected=False at confidence 0.0 — real absence of signal,
            # not fabricated agreement.
            opinions = {role: _screen_opinion(role, category) for role in _ROLES}
            o_values = {role: bool(op and op.suspected) for role, op in opinions.items()}
            composite_score, is_divergence = aggregate(
                o_values["temporal"], o_values["spatial"], o_values["procedural"], alpha=alpha_weights, threshold=threshold
            )
            assessments = [
                ErrorDetectionSubAgentAssessment(
                    agent_role=role,
                    tier_used=_FIXED_DEEP_TIER,  # schema has no "screen" tier — see CategoryResult docstring
                    error_present=o_values[role],
                    confidence=(opinions[role].confidence if opinions[role] else 0.0),
                    reasoning=(f"[screen-pass, not deep-reviewed] {opinions[role].observation}" if opinions[role] else "[screen-pass, not deep-reviewed] no opinion formed on this category"),
                    frames_examined=[window.start_frame, window.end_frame],
                )
                for role in _ROLES
            ]
            category_results[category] = CategoryResult(
                composite_score=composite_score, is_divergence=is_divergence, assessments=assessments, reviewed="screen"
            )

    fired = {c: r for c, r in category_results.items() if r.is_divergence}
    top_category = max(fired, key=lambda c: fired[c].composite_score) if fired else max(
        category_results, key=lambda c: category_results[c].composite_score
    )
    top_result = category_results[top_category]
    psi = compute_psi(top_category)  # informational — describes the top-scoring category, not a tier gate

    return ErrorDetectionWindowAssessment(
        window_id=window.window_id,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        sub_agent_assessments=top_result.assessments,
        escalated_category=escalated_category,
        psi=psi,
        tier_used=_FIXED_DEEP_TIER,
        composite_score=top_result.composite_score,
        threshold_used=threshold,
        is_divergence=bool(fired),
        category_results=category_results,
    )


async def run_error_detection_sweep(
    video_id: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    stride_s: float = 1.0,
    window_s: float = DEFAULT_WINDOW_S,
    max_concurrent_windows: int = 6,
    annotations: ErrorAnnotations | None = None,
    on_window_complete: Callable[[ErrorDetectionWindowAssessment], Awaitable[None]] | None = None,
) -> list[ErrorDetectionWindowAssessment]:
    """Runs the full detection pipeline over every window in [start_s, end_s).
    `annotations` is only used to derive the real sample-rate-based window
    grid (tools/sedmamba_labels.py) — never to decide any window's outcome;
    pass an already-loaded ErrorAnnotations to avoid re-reading the pickle
    on every call.

    `on_window_complete`, if given, is awaited as EACH window finishes (via
    asyncio.as_completed, not only after the whole sweep) — this is what
    lets a caller (agents/error_detection/agent.py) stream every sub-agent's real
    input/output onto the graph in real time, not just the final fired
    result batched at the end. The offline validation sweep
    (scripts/run_monitor_validation_sweep.py) doesn't pass this — it never
    touches the graph."""
    ann = annotations or load_error_annotations(video_id)
    windows = generate_windows(ann, window_s=window_s, stride_s=stride_s, start_s=start_s, end_s=end_s)

    semaphore = asyncio.Semaphore(max_concurrent_windows)

    async def bounded(window: ErrorDetectionWindow) -> ErrorDetectionWindowAssessment:
        async with semaphore:
            return await run_error_detection_window(video_id, window)

    tasks = [asyncio.ensure_future(bounded(w)) for w in windows]
    results: list[ErrorDetectionWindowAssessment] = []
    for coro in asyncio.as_completed(tasks):
        assessment = await coro
        results.append(assessment)
        if on_window_complete is not None:
            try:
                await on_window_complete(assessment)
            except Exception:
                # docs/agentic_workflow.md §10: a failed window means no error
                # nodes for THAT window and nothing downstream triggering — it
                # does not mean the case stops. A transient write failure took
                # down an entire sweep before this guard existed, so no error
                # node was ever written and the whole reasoning chain behind it
                # never ran. Logged, never swallowed silently.
                logger.exception(
                    "error_detection: on_window_complete failed for %s — dropping this window, sweep continues",
                    assessment.window_id,
                )
    return results


def build_divergence_events(case_id: str, assessment: ErrorDetectionWindowAssessment, phase: str) -> list[DivergenceEvent]:
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
                source="error_detection_agentic",
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
                source_agent="error_detection_coordinator",
                source_tool="run_error_detection_window",
            )
        )
    return events


class ErrorDetectionCoordinatorAgent(BaseAgent):
    """Real ADK BaseAgent coordinator — see module docstring for why this is
    a BaseAgent (not LlmAgent, not ParallelAgent) and why the actual per-
    window logic lives in the module-level functions above rather than only
    inside _run_async_impl."""

    def __init__(self, name: str = "error_detection_coordinator"):
        # Fresh, identically-configured instances — not _SCREEN_AGENTS.values()
        # (see that dict's own comment: reusing those singletons here breaks
        # the moment ErrorDetectionCoordinatorAgent is constructed a second time).
        super().__init__(name=name, sub_agents=[build_subagent(role, mode="screen") for role in _ROLES])

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        video_id = state.get("video_id")
        case_id = state.get("case_id", ctx.session.id)
        start_s = float(state.get("error_detection_start_s", 0.0))
        end_s = state.get("error_detection_end_s")
        end_s = float(end_s) if end_s is not None else None

        if not video_id:
            yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text="error_detection_coordinator: no video_id in session state, nothing to do")]))
            return

        # Deferred import: agents/error_detection/agent.py imports FROM this module
        # (ErrorDetectionWindowAssessment, build_divergence_events, run_error_detection_sweep),
        # so importing error_detection_case at module level here would be circular.
        # This is the one place the "proper" ADK-invocation path needs it —
        # calling run_error_detection_sweep directly (as an earlier version of this
        # method did) skips agent.py's on_window_complete callback entirely,
        # meaning Orchestrator invoking Error Detection through the standard ADK flow
        # would silently emit ZERO real-time graph traceability, unlike the
        # agent.py::error_detection_case path used everywhere else. Unify on the one
        # entry point that actually writes to the graph.
        from agents.error_detection.agent import error_detection_case

        fired = await error_detection_case(case_id, video_id, start_s=start_s, end_s=end_s)
        summary = f"Error Detection swept case {case_id}, {len(fired)} divergence(s) fired."
        yield Event(author=self.name, content=types.Content(role="model", parts=[types.Part.from_text(text=summary)]))
