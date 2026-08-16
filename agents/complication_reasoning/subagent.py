"""Complication Reasoning's two ADK agents — docs/agentic_workflow.md §3 agent 4.

TWO CALLS, NOT ONE, because the sequence genuinely has two steps with a real
network fetch between them:

  1. FORMULATE — read the error and the live case context, decide what question
     the literature should be asked. The agent composes this itself; there is
     no error-category-to-query table anywhere, because that lookup is exactly
     the hand-authored mapping the design forbids. What makes the query good is
     that it can name THIS patient's situation (a 46 mL prostate, partial
     nerve-sparing, a falling MAP) rather than the error category in the
     abstract.

  2. REASON — read the retrieved abstracts alongside the same context and name
     the complications actually worth worrying about, each with a confidence
     and an explicit pointer to which retrieved paper supports it.

Split this way rather than one call with a tool because these agents use
`output_schema`, and an ADK LlmAgent with a structured output schema does not
also carry tools — the same pattern Error Detection's sub-agents already
follow.

GROUNDING IS THE POINT. The reasoning step must not assert a complication it
cannot tie to retrieved evidence, and when retrieval returned nothing it must
say so rather than reaching for general knowledge and presenting it as
literature-backed. The verification gate later refuses to build an external
alert on an ungrounded complication, so an honest `evidence_backed=False` here
is what makes that gate meaningful instead of decorative.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from tools.gemini_model import new_agent_model


class LiteratureQuery(BaseModel):
    query: str  # the agent's own words, composed from live case context
    # A deliberately broader second attempt, also the agent's own. Europe PMC
    # requires every term, so a precise query can return literally nothing —
    # measured: eight words returned four results, ten returned zero. The agent
    # decides what to drop, because only it knows which terms carry the
    # clinical question and which are incidental detail. A deterministic
    # truncation here would strip terms blindly.
    broader_query: str
    rationale: str  # one sentence: why this is the right question to ask
    initial_concerns: list[str] = Field(default_factory=list)  # hypotheses to test against what comes back


class ComplicationCandidate(BaseModel):
    name: str  # short clinical name, e.g. "bladder neck contracture"
    confidence: float = Field(ge=0, le=1)
    mechanism: str  # ONE sentence: how this error leads to this complication
    patient_specific_factor: str  # what about THIS patient raises or lowers it
    supporting_citation_index: int | None = None  # index into the retrieved list, or None if unsupported
    evidence_backed: bool  # False = general knowledge only; never disguised as literature-backed


class ComplicationAssessment(BaseModel):
    candidates: list[ComplicationCandidate]
    reasoning: str  # ONE short paragraph tying the error, the patient and the evidence together


_QUERY_INSTRUCTION = """You are the Complication Reasoning Agent. A technique
error has just been detected during a robot-assisted radical prostatectomy, and
you are deciding what to ask the published literature about it.

You will be given the detected error, the synthetic patient profile, the recent
vitals trend, the current surgical activity, and any recent related errors.

Compose ONE literature search query that would surface evidence about the
downstream complications this specific error could cause in this specific
patient. Good queries name the anatomy and the mechanism, and where the patient
profile is genuinely relevant (prostate volume, nerve-sparing intent, BMI, a
vitals excursion) they reflect it. A query that just restates the error
category will return generic results and is a wasted retrieval.

Do not use boolean operators or field tags — this goes to a plain search API,
and EVERY term you include is required, so each extra word can only narrow the
result set. Measured against the real API: an eight-word query returned four
results and a ten-word one returned zero. Keep `query` to five to eight words.

Also give `broader_query`: the same clinical question stripped to its three or
four load-bearing terms, used only if the first returns nothing. You choose
what to drop, since you know which terms carry the question and which are
incidental detail.

Also list the specific complications you already suspect, so they can be tested
against what actually comes back. Being wrong here is fine and expected; the
retrieved evidence is what decides.

rationale: ONE sentence on why this is the right question."""


_REASON_INSTRUCTION = """You are the Complication Reasoning Agent. You asked the
literature a question about a detected surgical error, and the results are in
front of you.

You will be given the detected error, the synthetic patient profile, the recent
vitals trend, the current activity, recent related errors, and the retrieved
abstracts (numbered).

Name the complications genuinely worth surfacing to the surgical team. For each:
- name: the short clinical name, not a sentence.
- mechanism: ONE sentence on how this error leads to this complication.
- patient_specific_factor: what about THIS patient raises or lowers the risk.
  If nothing in the profile is genuinely relevant, say so plainly rather than
  inventing a connection.
- supporting_citation_index: the number of the retrieved abstract that actually
  supports this. Only cite a paper you can point to a real line of support in.
- evidence_backed: true ONLY if a retrieved abstract genuinely supports it.
  If you are drawing on general medical knowledge instead, set this false and
  leave supporting_citation_index null. Do NOT stretch a loosely related paper
  into a citation — an honest unsupported candidate is useful, a fabricated
  citation is worse than nothing and will be rejected downstream.
- confidence: your real confidence this complication is worth acting on here.

Return at most three candidates, ordered most concerning first. If the error is
genuinely unlikely to cause a meaningful complication in this patient, return an
empty list — that is a real and useful answer, not a failure.

If no abstracts were retrieved, reason from general knowledge and mark every
candidate evidence_backed=false.

reasoning: ONE short paragraph tying the error, this patient and the evidence
together. Not a restatement of the candidates."""


def build_query_agent() -> LlmAgent:
    return LlmAgent(
        name="complication_query",
        model=new_agent_model(),
        instruction=_QUERY_INSTRUCTION,
        output_schema=LiteratureQuery,
    )


def build_reasoning_agent() -> LlmAgent:
    return LlmAgent(
        name="complication_reasoning",
        model=new_agent_model(),
        instruction=_REASON_INSTRUCTION,
        output_schema=ComplicationAssessment,
    )
