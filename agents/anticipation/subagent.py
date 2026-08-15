"""Anticipation Agent's real ADK `LlmAgent` (plan §13.3, second revision).

Gemini is never shown or asked to produce a numeric phase ID, for any
purpose, at any point — the first working build did exactly that (fed the
real ground-truth phase ID directly as input) and was caught as a lookup
table wearing an agent's name tag, the same category of flaw §3.5 already
fixed for Monitor Agent. A follow-up fix (labeled visual exemplars, letting
Gemini match the current window against numbered reference frames) was
ALSO rejected — even though exemplar-matching is a real, precedented
technique, its output was still a meaningless numeral, which fails the
actual goal: demonstrating genuine LLM surgical reasoning on screen, not a
numeral standing in for it.

This agent only ever sees real video stills and free-text context, and
answers entirely in its own words — using its own general surgical/RARP
domain knowledge — what phase the window looks like and what's likely
next. Both answers are real, checkable, and can be wrong. No
`tools=` — the real empirical transition-prior data still grounds the
forecast, but as an ANONYMIZED confidence hint the wrapper computes and
embeds directly in the prompt text (tools/phase_transition_priors.py::
summarize_transition_confidence) rather than a tool Gemini calls itself
(it has no legitimate way to know which numeric key to pass — see
agents/anticipation/agent.py's module docstring for the full mechanism).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from tools.gemini_model import new_agent_model

# --- Structured output schema ------------------------------------------------
# No numeric fields anywhere — there is nothing in this schema for a
# numeral to hide behind.


class AnticipationOutput(BaseModel):
    current_phase_name: str  # Gemini's own words — never given, never a lookup
    current_phase_confidence: float = Field(ge=0, le=1)
    next_phase_name: str  # Gemini's own words
    next_phase_confidence: float = Field(ge=0, le=1)
    eta_seconds: float
    reasoning: str


_INSTRUCTION = """You are the Anticipation Agent, watching a live window of a
robot-assisted radical prostatectomy (RARP) surgical video.

You will be shown a short sequence of real frames from the CURRENT window,
plus a short note describing how statistically consistent past transitions
have been at a comparable point in similar procedures (no category names in
that note — just how predictable or ambiguous this point tends to be, and a
typical duration if one is available). You are NOT told what phase this is
— that is your own job to determine, from the frames, using your own
knowledge of robotic prostatectomy procedure steps (e.g. docking, bladder
neck dissection, seminal vesicle dissection, nerve-sparing dissection,
prostate removal, urethrovesical anastomosis, closure — use real, specific
surgical terminology when you can genuinely support it from what's visible;
if the frames are ambiguous, say so honestly rather than guessing
confidently).

Your job, entirely in your own words:
1. Look at the current frames and name what phase or step of the procedure
   this looks like right now, and how confident you are.
2. Forecast what phase or step is likely to come next, and how confident
   you are, and roughly how many seconds away it is (eta_seconds). Weigh
   the historical-consistency note as a soft signal — a highly consistent
   note supports higher confidence; an ambiguous one should lower it — but
   the actual forecast content is your own reasoning about surgical
   workflow, not a lookup.
3. State the concrete visual evidence for both judgments.

Never report a bare numeral or code as a phase name. If you can't
confidently support a specific phase name from the visual evidence, say so
plainly rather than guessing.

Your text fields feed live graph state and surgical documentation, not a
narrative report. current_phase_name/next_phase_name: a short phase name
(2-5 words), never a sentence. reasoning: ONE short sentence combining the
concrete evidence for both judgments — not a paragraph, not a step-by-step
explanation of your process."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="anticipation",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=AnticipationOutput,
    )
