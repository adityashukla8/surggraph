"""Scene Graph Builder's real ADK `LlmAgent` (initial_11082026.md §5.2 item
2): "takes windowed frames from the SAR-RARP50 video, calls Gemini vision
to emit structured JSON... writes nodes/edges into the Living Surgical
State graph."

A single agent (unlike Monitor's three-role split) — instrument/anatomy
identification and relation extraction is one perception task here, not
three independent specialist perspectives.

Ground-truth-as-input-context, decided plan §12: the real phase/action ID
(action_continuous.txt) is fed as input context — standing in for what a
genuine upstream perception/telemetry system would provide in a real
deployment — never as a shortcut for the agent's own output. The agent
still has to do all the actual interpretive work: what's actually in the
frame, what relation is happening, and how to describe the activity.
Ground truth here narrows *where to look*, never *what to say*.

The real segmentation mask (segmentation/) was dropped from this agent's
input as of docs/latency_optimization.md's restructuring — real, confirmed
per-image tiling cost with no matching latency benefit for this agent (see
that doc's Current §2). tools/segmentation_masks.py still exists and is
still used elsewhere (scripts/prepare_demo_videos.py's video overlay).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field
from typing import Literal

from tools.gemini_model import new_agent_model

# --- Structured output schema ----------------------------------------------


class SceneEntity(BaseModel):
    entity_id: str  # agent-assigned, short & stable, e.g. "needle_driver_right"
    entity_type: Literal["instrument", "anatomy"]
    label: str  # human-readable name, the agent's own real judgment
    confidence: float = Field(ge=0, le=1)


class SceneRelation(BaseModel):
    subject_entity_id: str
    verb: str  # e.g. "grasping", "suturing", "retracting" — the agent's own words
    target_entity_id: str | None = None
    confidence: float = Field(ge=0, le=1)


class SceneGraphWindowOutput(BaseModel):
    entities: list[SceneEntity]
    relations: list[SceneRelation]
    activity_description: str
    reasoning: str


# --- Agent construction ------------------------------------------------------

_INSTRUCTION = """You are the Scene Graph Builder Agent, observing a window of a
robot-assisted radical prostatectomy (RARP) surgical video.

Identify every surgical instrument and anatomical structure you can
actually see, the real relations between them (which instrument is acting
on which structure, and how), and describe the current surgical activity
in your own words.

A real signal may be included alongside the video window: a phase/gesture-
classification ID, a real, already-computed signal from an upstream
classifier. It is an OPAQUE numeric ID with no published semantic name
attached — treat it only as context for what kind of activity might
plausibly be happening, never as something to report back verbatim or
invent a name for.

Assign each entity a short, stable, descriptive entity_id (e.g.
'needle_driver_right', 'prostate') so the same real-world object can be
tracked consistently if it reappears in a later window's output. Only
report entities and relations you can actually support with a concrete
visual observation in the frames you were shown — do not guess or
hallucinate anything not actually visible.

Your text fields feed live graph state and surgical documentation, not a
narrative report — activity_description becomes a graph node's actual
label, so keep it to ONE short, precise phrase (roughly 5-8 words, e.g.
"suturing bladder neck with needle driver"), never a full sentence or
paragraph. reasoning should be one short sentence: the concrete evidence
for what you reported, not an explanation of your process. entity labels
and relation verbs should be a few words each, not descriptive clauses."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="scene_graph_builder",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=SceneGraphWindowOutput,
    )
