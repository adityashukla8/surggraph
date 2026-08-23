"""Corrective Replanning's ADK agent — docs/agentic_workflow.md §3 agent 6.

SELECTS, NEVER GENERATES. The agent is handed the bounded action library for
the triggering error's category and picks from it by `action_id`. It cannot
write clinical instructions of its own, because the schema has no field to put
them in — the output carries ids and orderings, and the human-readable action
text is looked up afterwards from the library.

That constraint is the whole safety argument for this step. Everything else in
the system reasons in free text, which is fine when the output is a description
or a hypothesis. Here the output is a suggestion about what a surgeon should do
next, and a model that can phrase that freely can also phrase something novel,
unsourced and wrong. Bounding the vocabulary means the worst case is the wrong
action from a reviewed list, not an invented one.

THE ESCALATE EXIT IS A REAL ANSWER. If nothing in the library genuinely fits,
the agent says so and proposes nothing. A forced weak match is worse than an
honest escalation, because a proposal on the graph is what divergence detection
later measures the surgeon against.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from tools.gemini_model import new_agent_model


class SelectedAction(BaseModel):
    action_id: str  # must exist in the library for this category; validated after the call
    order: int  # 1-based sequence position
    why_this_action: str  # ONE sentence tying it to THIS error and THIS patient


class CorrectiveProposal(BaseModel):
    # The escalate exit. When true, actions is empty and the case gets a
    # visible "no confident match" node instead of a weak proposal.
    escalate: bool
    escalation_reason: str = ""
    actions: list[SelectedAction] = Field(default_factory=list)
    summary: str = ""  # ONE short sentence naming the corrective intent overall
    urgency: str = "routine"  # routine | prompt | immediate


_INSTRUCTION = """You are the Corrective Replanning Agent in a robot-assisted
radical prostatectomy. A technique error was detected and complications have
been reasoned about. Your job is to propose what should happen next to reduce
the risk of those complications.

You will be given the triggering error, the complication candidates with any
supporting literature, the synthetic patient profile, the vitals trend, the
current activity, any proposal already active, and a numbered library of
corrective actions available for this error's category.

YOU MAY ONLY SELECT FROM THE LIBRARY. Return the action_id of each action you
are choosing, in the order they should be carried out. You cannot write an
action of your own — there is nowhere in the output to put one, and inventing
clinical instructions is exactly what this step is designed to prevent.

Choose the smallest set that genuinely addresses the risk. One well-chosen
action is better than three, and listing everything available is not a plan.

If nothing in the library genuinely fits this situation, set escalate=true,
give escalation_reason, and return no actions. That is a real and useful
answer. A forced weak match is worse than an honest escalation, because
whatever you propose is what the surgeon's actual next moves get compared
against.

If a proposal is already active and still applies, do not restate it — escalate
with a reason saying so.

For each selected action, why_this_action is ONE sentence connecting it to THIS
error and THIS patient. Not a restatement of the action text.

urgency: "immediate" if delay meaningfully worsens the risk, "prompt" if it
should happen within the current step, "routine" otherwise. Be honest — marking
everything immediate makes the signal useless.

summary: ONE short sentence naming the corrective intent overall."""


def build_subagent() -> LlmAgent:
    return LlmAgent(
        name="corrective_replanning",
        model=new_agent_model(),
        instruction=_INSTRUCTION,
        output_schema=CorrectiveProposal,
    )
