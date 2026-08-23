"""Documentation Agent's ADK agent — drafts the operative record.

docs/agentic_workflow.md §5 agent 11, docs/plan_v2 §6 step 12.

WHY THESE SECTIONS. A conventional operative note has a fixed anatomy —
findings, technique, complications, disposition — and most of it we genuinely
cannot fill: we do not know the specimen, the blood loss, the disposition, or
what the surgeon concluded. Reproducing that skeleton with our data poured into
it would produce a document that LOOKS like an operative note and is mostly
empty or inferred, which is worse than one that only claims what it knows.

So the sections here are the ones this system can actually support, and the one
section that is genuinely novel:

  PROCEDURE COURSE      the phase progression IS the operative narrative, and
                        it is directly observed rather than reconstructed.
  TECHNIQUE OBSERVATIONS the detected errors — but framed as automated,
                        unconfirmed observations, never as events that
                        occurred. See below; this is the critical distinction.
  RISKS CONSIDERED      complications the system reasoned about, with whether
                        each was literature-grounded. Documents the reasoning,
                        not an outcome.
  DECISION SUPPORT      what was proposed and how the surgeon responded. This
                        is the section a conventional note has no equivalent
                        for, and the one with the most real value: a record of
                        what the system raised and whether the team engaged.
  PHYSIOLOGICAL EVENTS  vitals excursions, which a real note would carry.

NOT INCLUDED: a benchmark/system-performance section. Earlier this ran after
benchmark_case and reported its score as a trust signal. That made
documentation depend sequentially on benchmarking for no real reason other
than that one field, so the two now run concurrently (orchestrator/agent.py)
and this agent never sees a benchmark result — nothing here is a substitute
trust signal for it.

DELIBERATELY ABSENT: a "Complications" heading. In an operative note that
heading means complications that OCCURRED. Ours are hypotheses a model
generated about what could follow from a detection that may itself be a false
positive. Putting them under that heading would be the single most dangerous
thing this document could do.

THE FRAMING RULE. Every clinical statement must be attributable. An error is
"automated analysis flagged X" — not "X happened". A complication is "the
system considered X" — not "X is expected". The surgeon has not confirmed any
of it, and the note must not imply otherwise anywhere.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from tools.gemini_model import new_agent_model


class OperativeNoteDraft(BaseModel):
    procedure_course: str  # the narrative of what was observed, in order
    technique_observations: str  # detected errors, framed as unconfirmed automated findings
    risks_considered: str  # complications reasoned about, with grounding status
    decision_support: str  # what was proposed and how the surgeon responded
    physiological_events: str  # vitals excursions, or an explicit statement of none
    summary: str  # 2-3 sentences a clinician would read first
    # Anything the graph could not support. Stated rather than silently omitted,
    # so a reader knows what this document does not cover.
    limitations: list[str] = Field(default_factory=list)


_INSTRUCTION = """You are drafting the automated portion of an operative record
for a robot-assisted radical prostatectomy. A surgeon will review it before
anything is filed.

You are given the complete case graph: the activities observed in order, the
technique errors the detector flagged, the complications reasoned about and
whether each was tied to real retrieved literature, the corrective plans
proposed and whether the surgeon acknowledged or dismissed them, any divergence
alerts, and physiological deviations.

THE FRAMING RULE, WHICH MATTERS MORE THAN ANYTHING ELSE HERE:

Every clinical statement must be attributable to what actually produced it.

  WRITE  "Automated analysis flagged possible out-of-view instrument handling
          at 0:20."
  NEVER  "The instrument was handled out of view at 0:20."

  WRITE  "The system considered bladder neck contracture as a downstream risk,
          supported by retrieved literature."
  NEVER  "The patient is at risk of bladder neck contracture."

Nothing here is surgeon-confirmed. A detection may be a false positive — this
document has no access to this case's self-benchmark score to know either way,
which is exactly why every finding must be framed as automated and unconfirmed
rather than asserted. A complication is a hypothesis a model generated, not a
finding. If you write a sentence a reader could take as confirmed clinical
fact, you have made the document unsafe.

SECTIONS:

- procedure_course: the phases observed, in order, with timings. This is the
  operative narrative and it is directly observed, so it can be stated
  plainly — "dissection of the bladder neck was observed from 0:15".

- technique_observations: the flagged errors, grouped sensibly, each with its
  OCHRA category and severity. Say plainly that these are automated and
  unconfirmed.

- risks_considered: complications the system reasoned about. For each, state
  whether it was literature-grounded or not. An ungrounded one is still worth
  recording as considered — say clearly that it was not evidence-supported.

- decision_support: what was proposed and what the surgeon did. Acknowledged,
  dismissed, or no response. If a proposal was diverged from, say so. This is
  the most useful section in the document — be concrete.

- physiological_events: vitals deviations with times. If there were none, say
  so explicitly rather than omitting the section.

- summary: two or three sentences a clinician would read first.

- limitations: what this record does not cover. Be specific — blood loss,
  specimens, disposition, surgeon-confirmed findings, and this case's own
  self-benchmark score are all absent, and the patient data is synthetic. Do
  not pad this with generalities.

Write in clinical register: precise, plain, no hedging beyond what the evidence
warrants and no drama. Every section is prose, not bullet fragments."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="documentation",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=OperativeNoteDraft,
    )
