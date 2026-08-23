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
    """A set of short, independent queries rather than one long one.

    Europe PMC is a boolean AND system: the query string is a CONSTRAINT, not a
    description. Every word is another clause the paper must literally contain.
    Measured on this project: going from 4 to 10 terms took a real clinical
    question from 608 hits to 1, and that single survivor was a conference
    abstract book — the only document dense enough to contain every term
    somewhere. Long queries do not rank badly, they starve the ranker.

    So the agent decomposes instead of elaborating, the way a medical
    librarian runs several narrow searches and triangulates rather than typing
    one long string.
    """

    queries: list[str] = Field(min_length=2, max_length=5)
    rationale: str  # one sentence: what angles these cover between them
    initial_concerns: list[str] = Field(default_factory=list)


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

Produce THREE TO FIVE SHORT, INDEPENDENT QUERIES — not one long one.

This is a boolean AND search, not a semantic search engine. Every word you
include is a clause the paper must literally contain somewhere. Adding a word
never refines the ranking; it deletes papers. Measured against this exact API:
a four-word query returned 608 real papers, and the same question at ten words
returned one irrelevant conference abstract book.

Treat every word as an assertion you are forcing the search to satisfy. If you
would be surprised to find that literal word in a relevant paper's abstract,
leave it out.

RULES:

1. NEVER exceed four content words per query. Hard cap. If a concept needs six
   words, it is two queries of three, not one of six.

2. Every word must be one you would expect literally in a relevant abstract.
   Framing words — "possible", "risk of", "in patients with", "post-operative"
   — are not search terms. They add clauses that delete real papers whose
   authors happened to phrase things differently.

3. Anchor on the NOUN FORM of the complication plus the procedure name. Those
   are the two load-bearing concepts. Everything else is a specialization.

4. Medical synonyms go in SEPARATE queries, never combined. "bladder neck
   contracture" and "vesicourethral anastomosis stricture" are the same
   clinical entity in overlapping literature; AND-ing them returns nothing,
   while running both roughly doubles coverage.

5. Decompose along INDEPENDENT AXES — complication/procedure,
   mechanism/outcome, anatomy/intervention. Each is a different way into the
   same literature. This is the PICO idea: different facets, searched
   separately.

6. NO patient descriptors unless they are genuinely discriminating. Appending
   "diabetic" to a query does not find papers about diabetic patients — it
   finds papers that happen to mention diabetes for unrelated reasons. If a
   comorbidity actually matters, give it its own query
   ("diabetes wound healing prostatectomy"), never as a modifier on another.

SYNTAX YOU SHOULD USE:

- QUOTE multi-word concepts: "bladder neck contracture" searches that exact
  phrase. Unquoted, those three words are AND-ed independently and may appear
  scattered anywhere in an unrelated paper.
- Field prefixes are available and precise:
    TITLE:"..."     the phrase must be in the title (very precise, low recall)
    ABSTRACT:"..."  in the abstract
    KW:"..."        in the paper's keywords
    MESH:"..."      the paper's MeSH indexing — the controlled vocabulary that
                    indexes it by topic regardless of the words the authors used
  A good precise query is KW:"radical prostatectomy" AND ABSTRACT:"bladder neck
  contracture". Use AT MOST ONE such field-scoped query in your set: measured
  here, combining MESH with KW collapsed a real question to a single hit, so
  field scoping buys precision at a real cost in recall.

A GOOD SET for excessive bladder-neck traction might be:
  "bladder neck contracture" "prostatectomy"        (complication + procedure)
  "vesicourethral anastomosis" stricture            (the synonym)
  "anastomotic leak" "radical prostatectomy"        (the sibling complication)
  KW:"radical prostatectomy" AND ABSTRACT:"bladder neck contracture"

Also list the complications you already suspect, so they can be tested against
what actually comes back. Being wrong is expected; the evidence decides.

rationale: ONE sentence on what angles these queries cover between them."""


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
