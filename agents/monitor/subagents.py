"""The three real Monitor sub-agents (plan §3.5): Temporal, Spatial,
Procedural. Each is an independent ADK `LlmAgent` making its own real
Gemini vision call over its own frame sample from the same window — no
`tools=`, pure structured vision reasoning (deliberately different from
the tool-calling pattern used by e.g. the Anticipation Agent, since here
the entire "tool" a call needs is the frames + knowledge block already in
the prompt — there's nothing further to look up mid-reasoning).

The coordinator (agents/monitor/coordinator.py) invokes these directly via
their own Runner, never through ADK's `sub_agents` LLM-delegation transfer
(that primitive means "hand off to exactly one," the wrong shape for
"always run all three and combine them" — see coordinator.py's docstring).

Invocation mechanics verified directly against the installed ADK 2.6.3
this session: `LlmAgent(model=..., instruction=..., output_schema=...)`
with no `tools=` is a valid construction; `InMemoryRunner(agent=...)` +
`await runner.session_service.create_session(...)` + `runner.run_async(
user_id=..., session_id=..., new_message=types.Content(...))` is the
confirmed local (non-deployed) invocation path, distinct from the
Day-1 spike's remote-deployed-agent path (`async_stream_query`).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from agents.monitor.knowledge import render_knowledge_block
from state.schema import ErrorCategory, ExpertiseTier, SubAgentRole
from tools.gemini_model import new_agent_model

# --- Structured output schemas -------------------------------------------
# Flat lists of category-tagged objects, not a dict keyed by category — more
# reliable for LLM structured-output APIs to produce than an enum-keyed dict.


class CategoryOpinion(BaseModel):
    category: ErrorCategory
    suspected: bool
    confidence: float = Field(ge=0, le=1)
    observation: str


class ScreenOutput(BaseModel):
    """Pass-1 (screen) output: an opinion per category the agent formed one
    on. An agent may omit a category entirely if it saw nothing relevant to
    it — omission is not the same as suspected=False with low confidence."""

    opinions: list[CategoryOpinion]


class DeepOutput(BaseModel):
    """Pass-2 (deep, risk-routed) output: a single focused binary call on
    the one escalated category, at the routed expertise tier's framing."""

    error_present: bool
    confidence: float = Field(ge=0, le=1)
    reasoning: str


# --- Role framing -----------------------------------------------------------

_ROLE_FOCUS: dict[SubAgentRole, str] = {
    "temporal": (
        "You are the TEMPORAL analysis agent. Focus on timing, motion, and sequence across "
        "the frames you're shown, which span the full window in chronological order: motion "
        "speed, hesitation, repeated/multi-attempt patterns, and whether the pacing of the "
        "action looks controlled versus rushed or stalled."
    ),
    "spatial": (
        "You are the SPATIAL analysis agent. Focus on positional accuracy in the frames you're "
        "shown: instrument tip placement, spacing between instruments, alignment relative to "
        "the anatomical target, and whether anything is positioned in a way that risks contact "
        "with the wrong structure."
    ),
    "procedural": (
        "You are the PROCEDURAL analysis agent. Focus on technique in the frames you're shown: "
        "whether the observed method matches expected surgical technique for this kind of "
        "step, correct tool use, and adherence to a sound procedural sequence."
    ),
}

_TIER_FRAMING: dict[ExpertiseTier, str] = {
    "resident": (
        "Apply a conservative, checklist-based approach: methodically check each indicator "
        "listed below against what you actually observe in the frames. When genuinely unsure, "
        "lean toward flagging for review rather than dismissing — but do not flag without a "
        "concrete observation to point to."
    ),
    "attending": (
        "Apply balanced clinical judgment: weigh the indicators below against the surgical "
        "context you can infer from the frames, considering both technical execution and its "
        "likely clinical significance."
    ),
    "expert": (
        "Apply sophisticated pattern synthesis: integrate subtle cues across the frames, "
        "considering technique nuance and edge cases a less experienced observer might miss "
        "or over-flag. Distinguish deliberate, controlled technique from a genuine deviation."
    ),
}


def _screen_instruction(role: SubAgentRole) -> str:
    return (
        f"{_ROLE_FOCUS[role]}\n\n"
        "You will be shown a sequence of frames from a ~10-second window of a robotic "
        "prostatectomy suturing video. For EACH of the six error categories below, decide "
        "whether you suspect that category of error is present in this window, based only on "
        "what you can actually observe in the frames — do not assert an error you can't point "
        "to a concrete visual observation for.\n\n"
        f"{render_knowledge_block()}\n\n"
        "Return one opinion per category you have a view on (omit a category entirely if you "
        "see nothing relevant to it, rather than guessing)."
    )


def _deep_instruction(role: SubAgentRole, category: ErrorCategory, tier: ExpertiseTier) -> str:
    return (
        f"{_ROLE_FOCUS[role]}\n\n"
        "You will be shown a sequence of frames from a ~10-second window of a robotic "
        "prostatectomy suturing video. A screening pass flagged this window as a candidate for "
        f"the '{category}' error category. Now give it a focused, deeper look.\n\n"
        f"{_TIER_FRAMING[tier]}\n\n"
        f"{render_knowledge_block([category])}\n\n"
        "Decide whether this specific error is genuinely present, based only on concrete "
        "observations in the frames."
    )


def build_subagent(
    role: SubAgentRole,
    mode: str,  # "screen" | "deep"
    tier: ExpertiseTier = "resident",
    category: ErrorCategory | None = None,
) -> LlmAgent:
    if mode == "screen":
        return LlmAgent(
            name=f"monitor_{role}_screen",
            model=new_agent_model(),
            instruction=_screen_instruction(role),
            output_schema=ScreenOutput,
        )
    if mode == "deep":
        if category is None:
            raise ValueError("deep mode requires a category")
        return LlmAgent(
            name=f"monitor_{role}_deep_{category}",
            model=new_agent_model(),
            instruction=_deep_instruction(role, category, tier),
            output_schema=DeepOutput,
        )
    raise ValueError(f"unknown mode: {mode!r}")


# Native-video sampling density per role, applied to the DEEP (accurate,
# expensive) tier only as of the latency restructuring (docs/latency_optimization.md)
# — was applied to both tiers before that. Temporal wants dense sampling to
# see motion/hesitation trend (fps=5.0 matches the benchmarked accuracy
# sweet spot for motion-sensitive tasks); Spatial and Procedural want fewer
# frame-equivalents over the same 10s window, mirroring their original
# "fewer frames" intent (media_resolution tuning for Spatial's positional-
# precision need is an open follow-up, not yet benchmarked).
VIDEO_FPS_PROFILE: dict[SubAgentRole, float] = {
    "temporal": 5.0,
    "spatial": 0.4,
    "procedural": 0.6,
}

# Still-frame sampling for the SCREEN (cheap, fast) tier — real, previously-
# designed values from before the native-video migration, reused now that
# the screen tier is back to still frames for latency (docs/latency_optimization.md,
# confirmed ~2.9x faster than native video for the same window). resize_to
# of None means native resolution.
SCREEN_STILL_FRAME_PROFILE: dict[SubAgentRole, dict] = {
    "temporal": {"n_frames": 10, "resize_to": (960, 540)},
    "spatial": {"n_frames": 4, "resize_to": None},
    "procedural": {"n_frames": 6, "resize_to": None},
}
