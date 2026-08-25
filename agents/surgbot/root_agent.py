"""SurgBot's root agent — the ONE deliberate tools=[...] exception in this
codebase.

Every other agent here gets a pre-sliced, deterministic prompt and returns
one fixed structured output (verified: tests/test_anticipation_agent.py::
test_anticipation_agent_has_no_tools asserts AGENT.tools == [] as a real,
tested convention across this project). SurgBot's job is structurally
different: a live, surgeon-driven conversation whose next question can't be
predicted or pre-sliced ("show me a different case," "skip to the corrective
proposals," "what about that divergence alert"). Giving the model real tools
and letting it decide when to call them is the only way that works — this is
disclosed here explicitly, the same way this codebase discloses every other
deliberate exception (e.g. World Model's hand-authored rules), and
tests/test_surgbot_root_agent.py is the regression guard that keeps this
honest in the other direction (root_agent must keep real tools; every OTHER
SurgBot subagent must keep tools == []).

build_root_agent() is never run locally by services/surgbot_service — it is
deployed to GEAP Agent Runtime and invoked remotely via a real
bidi_stream_query connection (scripts/deploy_surgbot_agent.py,
services/surgbot_service/main.py). Wrapped in
vertexai.preview.reasoning_engines.AdkApp (NOT vertexai.agent_engines.AdkApp
— the STABLE class has no bidi support at all, confirmed by reading its
source this session), deployed with agent_server_mode=EXPERIMENTAL and NO
identity_type (AGENT_IDENTITY breaks this agent's own outbound Live API call
— see agents/surgbot/live_model.py and scripts/deploy_surgbot_agent.py for
the full, empirically-confirmed account).

DISCLOSURE, STRUCTURAL NOT COSMETIC: this agent's own reasoning voice runs on
a Live API model that is NOT Gemini 3.5+ (no such model exists with
function-calling support today — see live_model.py) — every actual reasoning
step (error-chain review, synthesis, cross-session pattern insight) is
dispatched to a separately-deployed, real Gemini-3.5 Agent Runtime subagent
(agents/surgbot/subagents.py) instead. TOOL_DISCLOSURE and TOOL_PHASE_MAP
below are the static tables services/surgbot_service/main.py's relay reads to
emit the mandatory ToolUseDisclosure (agents/surgbot/schema.py) and
phase_changed events over the WebSocket control channel — every tool call
must resolve to a real agent_name + model_id + api_surface triple, never a
partial one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from google.adk.agents import Agent

from agents.surgbot import case_index, memory_bank, slices, store, subagents
from agents.surgbot.graph_reader import build_index, get_case_indexes
from agents.surgbot.live_model import SURGBOT_LIVE_MODEL, new_live_agent_model
from agents.surgbot.model_armor import screen_review_document
from agents.surgbot.schema import CaseReviewDocument, ReviewFeedbackItem

# Same real bug found in services/surgbot_service/main.py this session,
# confirmed here too via Cloud Logging: this module is what actually runs
# INSIDE the deployed Agent Runtime sandbox (AdkApp wraps build_root_agent(),
# and this file is re-imported there via extra_packages=["agents",...]) —
# and nothing anywhere set up a root logger for that process either. Every
# logger.info() in this module and in subagents.py/store.py (all imported
# here) was being silently dropped, which is exactly why an 8+-minute
# stretch of a real deployed session showed zero log output in Cloud
# Logging even though subagents.py has logger.info() calls on the exact
# code path that ran. Set up once here, at the real deployed entrypoint.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy_logger in ("httpx", "httpcore", "google", "google.auth", "urllib3", "grpc"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Real Gemini-3.5 identity every subagent tool call actually runs on —
# tools/gemini_model.py::GEMINI_MODEL, read once here rather than imported
# directly so this module has no hard import-time dependency on that file's
# env var resolution succeeding inside a deployed sandbox that might not have
# GEMINI_MODEL set (env_vars are passed explicitly at deploy time instead —
# see scripts/deploy_surgbot_subagents.py).
_SUBAGENT_MODEL_ID = "gemini-3.5-flash"

TOOL_DISCLOSURE: dict[str, dict[str, str]] = {
    "list_accessible_cases": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "get_error_statistics_across_cases": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "load_case_graph": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "get_phase_detail": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "review_error_chain": {"agent_name": "surgbot_error_chain_reviewer", "model_id": _SUBAGENT_MODEL_ID, "api_surface": "vertex_ai_global"},
    "review_proposal_divergence": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "record_feedback": {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"},
    "draft_review_document": {"agent_name": "surgbot_synthesis", "model_id": _SUBAGENT_MODEL_ID, "api_surface": "vertex_ai_global"},
    "retrieve_reviewer_patterns": {"agent_name": "surgbot_pattern_insight", "model_id": _SUBAGENT_MODEL_ID, "api_surface": "vertex_ai_global"},
}

# Which review phase each tool call signals the conversation has reached —
# services/surgbot_service/main.py emits phase_changed whenever a tool call's
# mapped phase differs from the last one it announced. Tools not listed here
# (record_feedback) don't move the phase forward on their own.
TOOL_PHASE_MAP: dict[str, int] = {
    "list_accessible_cases": 1,
    "load_case_graph": 1,
    "get_phase_detail": 2,
    "review_error_chain": 3,
    "review_proposal_divergence": 4,
    "draft_review_document": 5,
    "retrieve_reviewer_patterns": 6,
}


# --- Tools --------------------------------------------------------------------
# All plain async functions, per this file's own deliberate exception to the
# rest of the codebase's tools=[] convention. Each returns plain JSON-
# serializable data (dict/list/str) — ADK's function-calling layer handles
# turning that into a tool result the Live model can speak/reason about.


async def list_accessible_cases() -> dict[str, Any]:
    """Lists every case currently visible to SurgBot for cross-case review.

    Cross-case access here means every case in the project's Firestore —
    there is no per-user ACL system in this build (disclosed simplification,
    plan §14.1). Returns an honest empty list if no cases exist yet; never
    fabricates a case to make the list look non-empty.
    """
    # case_index.list_cases() is a plain synchronous Firestore call — same
    # event-loop-freezing class of bug found and fixed elsewhere in this
    # file this session. Run it in a thread.
    cases = await asyncio.to_thread(case_index.list_cases)
    return {"cases": [c.model_dump(mode="json") for c in cases], "count": len(cases)}


_MAX_STATS_CASES = 40  # real latency cap, disclosed in the tool's own return payload — not a fabricated sample size


async def get_error_statistics_across_cases() -> dict[str, Any]:
    """Cross-case analytical question, real user need (not tied to any single
    phase — call this any time the reviewer asks something like "what's the
    most erroneous case", "what's the most common error type across cases",
    "which case has the most complications"): fetches the REAL graph for
    multiple cases in parallel and computes real error-category counts and
    per-case error counts. A plain deterministic computation over real data
    (matches this codebase's own house rule against disguising deterministic
    work as agentic) — no LLM call needed, no subagent dispatch, and nothing
    here is estimated or fabricated.

    Real cost, disclosed in the response itself: fetching every case's full
    graph one at a time would make a cross-case question take too long
    mid-conversation, so this samples at most _MAX_STATS_CASES cases (fetched
    concurrently) rather than the full corpus — cases_analyzed vs.
    total_cases_available in the response tells you exactly how partial the
    answer is. Speak that caveat if the reviewer is drawing a conclusion from
    a sample rather than the whole set.
    """
    all_cases = await asyncio.to_thread(case_index.list_cases)
    sample = all_cases[:_MAX_STATS_CASES]
    indexes = await get_case_indexes([c.case_id for c in sample])

    per_case_error_counts: dict[str, int] = {}
    error_category_counts: dict[str, int] = {}
    per_case_complication_counts: dict[str, int] = {}
    for case_id, index in indexes.items():
        errors = index.of_type("error")
        per_case_error_counts[case_id] = len(errors)
        per_case_complication_counts[case_id] = len(index.of_type("complication"))
        for error_node in errors:
            category = error_node.attrs.get("error_category", "unknown")
            error_category_counts[category] = error_category_counts.get(category, 0) + 1

    most_erroneous = max(per_case_error_counts.items(), key=lambda kv: kv[1], default=None)
    most_complications = max(per_case_complication_counts.items(), key=lambda kv: kv[1], default=None)
    most_common_category = max(error_category_counts.items(), key=lambda kv: kv[1], default=None)

    return {
        "cases_analyzed": len(sample),
        "total_cases_available": len(all_cases),
        "sample_is_partial": len(sample) < len(all_cases),
        "per_case_error_counts": per_case_error_counts,
        "most_erroneous_case": (
            {"case_id": most_erroneous[0], "error_count": most_erroneous[1]} if most_erroneous else None
        ),
        "per_case_complication_counts": per_case_complication_counts,
        "case_with_most_complications": (
            {"case_id": most_complications[0], "complication_count": most_complications[1]}
            if most_complications
            else None
        ),
        "error_category_counts": error_category_counts,
        "most_common_error_category": (
            {"category": most_common_category[0], "count": most_common_category[1]}
            if most_common_category
            else None
        ),
    }


async def load_case_graph(case_id: str) -> dict[str, Any]:
    """Phase 1 (case framing): loads the whole-case orientation slice for one
    case — patient twin, phase list, error/complication counts, active
    corrective proposals, divergence alerts, benchmark and documentation
    status if present."""
    index = await build_index(case_id)
    return slices.case_framing_slice(index)


async def get_phase_detail(case_id: str, phase_node_id: str) -> dict[str, Any]:
    """Phase 2 (phase-by-phase walkthrough): loads one surgical phase's
    detail — the phase node itself, plus the errors and complications
    detected during its window."""
    index = await build_index(case_id)
    return slices.phase_walkthrough_slice(index, phase_node_id)


async def review_error_chain(case_id: str, error_node_id: str) -> dict[str, Any]:
    """Phase 3 (error-and-complication review): dispatches the real, causal
    chain rooted at one error (the error, its linked complications, and any
    literature evidence already attached) to the deployed error_chain_reviewer
    subagent (a real, separate Agent Runtime deployment, Gemini 3.5) for a
    mechanism summary, plausibility probe, and citation summary."""
    index = await build_index(case_id)
    chain = slices.error_chain_slice(index, error_node_id)
    if not chain.get("found"):
        return {"found": False, "error_node_id": error_node_id}
    result = await subagents.invoke_subagent("error_chain_reviewer", json.dumps(chain))
    return {"found": True, "chain": chain, "review": result.get("parsed"), "review_error": result.get("error")}


async def review_proposal_divergence(case_id: str, proposal_id: str) -> dict[str, Any]:
    """Phase 4 (proposal-and-divergence review): loads one corrective
    proposal plus every divergence_alert raised against it, so the reviewer
    can weigh whether the proposal was sound and whether flagged
    divergences were justified. Deterministic slice, no subagent dispatch —
    this is a judgment the human reviewer makes directly with SurgBot in
    conversation, not one delegated to a second model."""
    index = await build_index(case_id)
    return slices.proposal_divergence_slice(index, proposal_id)


async def record_feedback(
    session_id: str,
    phase: int,
    case_id: str,
    subject_node_id: str,
    verdict: str = "",
    rationale: str = "",
    coaching_note: str = "",
) -> dict[str, Any]:
    """Records one piece of structured reviewer feedback against the running
    session — the mechanism that makes this a genuine Track 2 "captures
    feedback so it constantly adapts" agent rather than a read-only tour.
    verdict must be "agree", "disagree", "uncertain", or "" (no verdict given
    yet, e.g. a coaching note with nothing to agree/disagree with)."""
    normalized_verdict = verdict if verdict in ("agree", "disagree", "uncertain") else None
    item = ReviewFeedbackItem(
        phase=phase,  # type: ignore[arg-type]  # validated by ReviewFeedbackItem's own Literal
        case_id=case_id,
        subject_node_id=subject_node_id,
        verdict=normalized_verdict,
        rationale=rationale,
        coaching_note=coaching_note,
    )
    await store.append_session_feedback(session_id, item)
    return {"recorded": True, "phase": phase, "case_id": case_id, "subject_node_id": subject_node_id}


async def draft_review_document(session_id: str) -> dict[str, Any]:
    """Phase 5 (synthesis and approval): gathers the session's case framing
    for every case in scope plus all recorded feedback, dispatches it to the
    deployed synthesis subagent for a real drafted case-review document,
    screens the drafted narrative through agents/surgbot/model_armor.py
    (fail-closed — a screening failure blocks the draft from ever reaching
    approval_status=pending), and persists it via agents/surgbot/store.py.
    """
    session = await store.get_session(session_id)
    if session is None:
        return {"drafted": False, "error": f"no SurgBot session {session_id!r}"}

    indexes = await get_case_indexes(session.case_ids)
    framings = {cid: slices.case_framing_slice(idx) for cid, idx in indexes.items()}
    feedback_items = await store.get_session_feedback(session_id)

    payload = {
        "case_ids": session.case_ids,
        "reviewer_id": session.reviewer_id,
        "case_framings": framings,
        "feedback_items": [f.model_dump(mode="json") for f in feedback_items],
    }
    result = await subagents.invoke_subagent("synthesis", json.dumps(payload))
    draft = result.get("parsed") or {}

    review_id = session.review_id or f"surgbot-review-{uuid.uuid4().hex[:12]}"
    text_to_screen = "\n\n".join(
        str(v)
        for v in (
            draft.get("case_summary", ""),
            *draft.get("agreements", []),
            *draft.get("disagreements", []),
            *draft.get("coaching_notes", []),
        )
        if v
    )
    # screen_review_document() is a plain synchronous Model Armor network
    # call — real bug found this session: calling it directly here would
    # freeze the whole event loop (no Live API keepalive/audio) for however
    # long that call takes, the same class of bug just fixed in
    # subagents.invoke_subagent(). Run it in a thread.
    screen = await asyncio.to_thread(screen_review_document, text_to_screen) if text_to_screen else None

    approval_status = "blocked" if (screen and screen.blocked) else "pending"
    document = CaseReviewDocument(
        review_id=review_id,
        case_id=session.case_ids[0] if session.case_ids else "",
        session_id=session_id,
        reviewer_id=session.reviewer_id,
        approval_status=approval_status,  # type: ignore[arg-type]
        case_summary=draft.get("case_summary", ""),
        follow_up_items=draft.get("follow_up_items", []),
        agreements=draft.get("agreements", []),
        disagreements=draft.get("disagreements", []),
        coaching_notes=draft.get("coaching_notes", []),
        threshold_adjustments=draft.get("threshold_adjustments", []),
        sections=draft,
        model_armor_reason=(screen.reason if screen else None),
        feedback_items=feedback_items,
    )
    await store.save_review_draft(document)
    await store.update_session(session_id, review_id=review_id)

    return {
        "drafted": True,
        "review_id": review_id,
        "approval_status": approval_status,
        "sections": draft,
        "synthesis_error": result.get("error"),
    }


async def retrieve_reviewer_patterns(reviewer_id: str) -> dict[str, Any]:
    """Phase 6 (cross-session pattern review): retrieves this reviewer's real
    Memory Bank facts from past SurgBot sessions and dispatches them to the
    deployed pattern_insight subagent for an honest cross-session framing —
    an empty memory history renders as "no prior history yet", never a
    fabricated pattern claim (plan §14.6's cut-order explicitly calls this
    out: degrade to single-session stats before ever asserting unsupported
    cross-session claims)."""
    # Both deploy_or_get_subagent() and memory_bank.retrieve_memories() are
    # plain synchronous, blocking network calls — same event-loop-freezing
    # bug fixed elsewhere in this file this session. Run both in a thread.
    synthesis_engine = await asyncio.to_thread(subagents.deploy_or_get_subagent, "synthesis")
    facts = await asyncio.to_thread(
        memory_bank.retrieve_memories,
        reviewer_id,
        query="review session patterns",
        agent_engine=synthesis_engine.api_resource.name,
    )
    payload = {"reviewer_id": reviewer_id, "memories": facts}
    result = await subagents.invoke_subagent("pattern_insight", json.dumps(payload))
    return {"memory_count": len(facts), "memories": facts, "insight": result.get("parsed"), "insight_error": result.get("error")}


_TOOLS = [
    list_accessible_cases,
    get_error_statistics_across_cases,
    load_case_graph,
    get_phase_detail,
    review_error_chain,
    review_proposal_divergence,
    record_feedback,
    draft_review_document,
    retrieve_reviewer_patterns,
]


_INSTRUCTION = """You are SurgBot, a conversational, voice-driven, cross-case surgical
case review assistant. You talk with a surgeon or QA/patient-safety reviewer
about one or more COMPLETED surgical cases already processed by the SurgGraph
pipeline. You are a Collaborative Partner, not an autonomous pipeline: ask
clarifying questions, guide the reviewer step by step through a fixed review
script, and actively capture their feedback so it becomes a real, structured
record — never just narrate the graph back at them.

MANDATORY: you receive a silent, non-conversational session_id message when
the connection opens — it is NOT a prompt to speak, and you MUST NOT respond
to it or say anything at all until the reviewer actually speaks first. Stay
completely silent until you hear the reviewer's own first words. 

When asked who are you/what can you do: introduce yourself and your capabilities in a quick 1-2
sentences and mention that since all surgeons have different ways of
operating, you'll take the feedback throughout the session and incorporate
that into your long-term memory — then continue naturally into whatever the
reviewer actually asked or said.

You run a FIXED SIX-PHASE SCRIPT:

PHASE 1 — Case framing. Call list_accessible_cases to see what's available if
the reviewer hasn't already named a case. Call load_case_graph for the case(s)
in scope. Orient the reviewer: summarize the case at a glance (how many
phases, errors, complications, active corrective proposals, divergence
alerts) before drilling into anything.

PHASE 2 — Phase-by-phase walkthrough. For each surgical phase in the case
(from the case framing you already loaded), call get_phase_detail and walk
the reviewer through what was detected during that phase. Ask whether they
agree with what the system flagged for that phase, and call record_feedback
with their verdict (agree/disagree/uncertain) and any rationale they give.

PHASE 3 — Error-and-complication review. For each error worth discussing
(especially ones linked to a complication), call review_error_chain — this
dispatches to a real Gemini 3.5 subagent for a mechanism summary,
plausibility probe, and citation summary. Present that to the reviewer, ask
whether they agree with the causal chain and any coaching note they'd add,
and record their feedback.

PHASE 4 — Proposal-and-divergence review. For each active corrective proposal,
call review_proposal_divergence to see the proposal and any divergence alerts
raised against it. Ask the reviewer whether the proposal was sound and
whether the divergence alerts (if any) were justified or false positives.
Record their feedback, including any suggested threshold_adjustments.

PHASE 5 — Synthesis and approval. Once you've covered the phases, errors, and
proposals the reviewer wants to discuss, call draft_review_document. This
dispatches to a real Gemini 3.5 synthesis subagent and screens the result
through Model Armor before it's ever shown as approvable. Read the drafted
case_summary, agreements, disagreements, coaching_notes, and follow_up_items
back to the reviewer. Tell them the review document is now available in the
UI for them to approve, edit, or reject — you do not approve it yourself,
that is always a human action taken outside this conversation.

PHASE 6 — Cross-session pattern review. Call retrieve_reviewer_patterns for
the reviewer. If they have no prior session history, say so plainly — do not
imply a pattern exists when the tool tells you has_history is false. If real
patterns are found, share them as genuine cross-session coaching insight, not
as this-session-only observations.

CROSS-CASE ANALYTICAL QUESTIONS — not tied to any one phase, answerable any
time: if the reviewer asks something like "what's the most erroneous case",
"what's the most common error type across cases", or "which case has the
most complications", call get_error_statistics_across_cases. It samples a
real subset of cases (never the whole corpus, for latency) — its response
tells you cases_analyzed vs. total_cases_available and sample_is_partial; if
sample_is_partial is true, say so plainly when you answer ("out of the first
N cases I checked" or similar), never imply the answer covers every case
when it only covers a sample.

GENERAL RULES:
- NEVER speak a case_id, node_id, or any other opaque identifier out loud
  character-by-character (e.g. "case-2f4a872f4b6f" spoken as "case, two, f,
  four, a..."). These are for your own tool calls only. When talking to the
  reviewer, refer to cases by ordinal or position instead — "the most recent
  case", "the first case in the list", "the third one you mentioned" — and
  only fall back to reading an id aloud, once, plainly as a whole word/code
  (not spelled out), if the reviewer explicitly asks for the literal id.
- Always ground what you say in the actual data returned by your tools.
  Never invent case facts, error details, literature citations, or memory
  history that your tools did not actually return.
- Ask real clarifying questions when the reviewer's intent is ambiguous
  ("which case do you mean", "do you want to skip ahead to proposals").
  Follow the reviewer if they want to jump around the script (e.g. "skip to
  the corrective proposals") — the fixed phase numbering is your default
  path, not a cage.
- Every piece of reviewer feedback (agreement, disagreement, coaching note)
  should be captured via record_feedback as it's given, not just summarized
  at the end.
- record_feedback and draft_review_document both require the exact
  session_id you were given at the very start of this conversation (in the
  "[session_init] session_id=..." message) — always pass that same value,
  never a case_id, reviewer_id, or a value you invent.
- Keep spoken responses natural and concise — you are a voice conversation,
  not a document."""


def build_root_agent() -> Agent:
    """Builds SurgBot's root agent. NOT run locally — deployed to GEAP Agent
    Runtime (scripts/deploy_surgbot_agent.py) and invoked remotely via
    bidi_stream_query (services/surgbot_service/main.py)."""
    return Agent(
        name="surgbot_root",
        model=new_live_agent_model(),
        instruction=_INSTRUCTION,
        tools=_TOOLS,
    )
