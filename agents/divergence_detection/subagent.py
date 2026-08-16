"""Trajectory Divergence Detection's ADK agent — the semantic fallback only.

docs/agentic_workflow.md §3 agent 7 specifies deterministic-first with an
LLM fallback, and this module is the fallback half. The deterministic half
lives in agent.py and answers the questions that have a real yes/no answer in
graph state — did the error this proposal addresses fire again, has enough of
the case elapsed to judge at all.

This call handles the question that genuinely does not: whether what the
surgeon is actually doing is consistent with the SPIRIT of the proposal. A
proposal says "re-centre the endoscope so all working tips are in frame"; the
perception stream says "adjusting suture needle with needle driver". Deciding
whether the second satisfies the first is a semantic judgment, and pretending
otherwise with string matching would produce confident nonsense.

Being unable to tell is a real answer here. `aligned=None` means the evidence
does not support a judgment either way, and the caller writes nothing rather
than guessing — a false divergence alert costs a surgeon's attention during an
operation, which is the most expensive thing this system can spend.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field


from tools.gemini_model import new_agent_model


class DivergenceJudgment(BaseModel):
    # True = following the proposal, False = diverging, None = cannot tell.
    aligned: bool | None
    confidence: float = Field(ge=0, le=1)
    # Which verification checks the observed activity actually satisfies, by
    # step order. Forces the judgment to point at specific evidence rather
    # than being a vibe about the whole proposal.
    satisfied_steps: list[int] = Field(default_factory=list)
    unsatisfied_steps: list[int] = Field(default_factory=list)
    reasoning: str  # ONE short sentence citing the concrete observation


_INSTRUCTION = """You are the Trajectory Divergence Detection Agent in a
robot-assisted radical prostatectomy.

A corrective plan was proposed to the surgical team. You are deciding whether
what the surgeon is ACTUALLY doing is consistent with it.

You will be given the proposal with its numbered steps and each step's
verification check, plus the recent stream of what perception actually
observed: activity descriptions, instruments entering and leaving the field,
and relations between them.

For each step, decide whether the observed activity satisfies its verification
check. List the step orders under satisfied_steps and unsatisfied_steps.

Then judge overall:
- aligned=true if the observed activity is consistent with the proposal's
  intent, even if the surgeon's exact motions differ from the wording. You are
  judging the spirit, not matching text.
- aligned=false if the observed activity clearly continues the behaviour the
  proposal asked to change.
- aligned=null if the observations genuinely do not let you tell. This is a
  real answer and you should use it. A false divergence alert interrupts a
  surgeon mid-operation, so silence is much better than a guess.

Perception is deliberately quiet: it only reports when something changed. A
short list of observations usually means the case is proceeding steadily, NOT
that the surgeon is ignoring the plan. Absence of evidence is not divergence.

reasoning: ONE short sentence citing the concrete observation behind your
judgment. Not a restatement of the proposal."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="divergence_detection",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=DivergenceJudgment,
    )
