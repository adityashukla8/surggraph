"""The Perception Sweep Agent's real ADK `LlmAgent`.

One agent, not a role split: identifying instruments and anatomy, reading the
relations between them, and naming the current activity is a single perception
task, unlike Error Detection where three independent specialist perspectives
are the point.

WHAT THIS CALL IS RESPONSIBLE FOR, AND WHAT IT IS NOT. This produces one raw
observation per window and nothing else. It has no memory of previous windows,
makes no judgment about whether anything CHANGED, and never decides what
reaches the graph — that is agents/perception/pipeline.py's deterministic
change-diff layer. Keeping the split clean is what lets the model answer the
question it is actually good at ("what do you see right now") without also
being asked to remember and diff, which it would do inconsistently.

STABLE ENTITY IDS ARE THE ONE THING THIS CALL MUST GET RIGHT ACROSS WINDOWS.
The registry is keyed by the id the model assigns, so a needle driver that
comes back as `needle_driver_right` in one window and `right_needle_driver`
in the next becomes two entities that both look real. The instruction is
explicit about this, and the previous window's active entity ids are supplied
in the context slice precisely so the model can reuse them verbatim.

Ground truth as input context: the real phase/action ID may accompany the
window as an OPAQUE numeric id with no semantic name attached — the same
standing rule as elsewhere. It narrows where to look, never what to say. The
model still does all the interpretive work and can be wrong.
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from tools.gemini_model import new_agent_model


class PerceivedEntity(BaseModel):
    stable_id: str  # model-assigned, canonical, reused verbatim across windows
    kind: Literal["instrument", "anatomy", "material"]
    label: str  # human-readable, the model's own judgment
    confidence: float = Field(ge=0, le=1)


class PerceivedRelation(BaseModel):
    subject_id: str
    verb: str  # "grasping", "retracting", "suturing" — the model's own words
    target_id: str
    confidence: float = Field(ge=0, le=1)


class PerceptionWindowOutput(BaseModel):
    entities: list[PerceivedEntity]
    relations: list[PerceivedRelation]
    activity_description: str
    reasoning: str


_INSTRUCTION = """You are the Perception Agent, observing one short window of a
robot-assisted radical prostatectomy (RARP) surgical video.

Report what you can actually see in THIS window:
- Every surgical instrument, anatomical structure, and material (suture, clip,
  gauze) actually visible in the frames.
- The real relations between them — which instrument is acting on which
  structure, and how.
- The current surgical activity, in your own words.

STABLE IDS MATTER MORE THAN ANYTHING ELSE HERE. Each entity gets a short,
lowercase, underscore-separated stable_id describing what it is, e.g.
'needle_driver_right', 'prostate', 'suture_1'. You will be shown the entity ids
already active in this case. If you see the same real-world object again, REUSE
ITS EXISTING ID EXACTLY — do not invent a new spelling, do not reorder the
words, do not add or drop a qualifier. A different id means a different object,
and duplicating one instrument under two ids corrupts the case record. Only
mint a new id for something genuinely not in that list.

Report only what a concrete visual observation supports. If an instrument is
occluded or you cannot tell what a structure is, leave it out rather than
guessing — an omission is recoverable, a confident wrong entity is not.

You may be given an opaque numeric phase/gesture id from an upstream
classifier. It has no published semantic name. Treat it only as a hint about
what kind of activity might plausibly be happening. Never report it back and
never invent a name for it.

Your text feeds live graph state and a surgical record, not a narrative report:
- activity_description: ONE short precise phrase, roughly 5-8 words, e.g.
  "suturing bladder neck with needle driver". Never a sentence or paragraph.
  Describe the activity itself, not your confidence in it.
- reasoning: ONE short sentence giving the concrete visual evidence for what
  you reported. Not an account of your process.
- entity labels and relation verbs: a few words each, never clauses."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="perception",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=PerceptionWindowOutput,
    )
