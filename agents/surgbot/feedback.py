"""SurgBot's write path for the surgeon-feedback knowledge base (plan_v2
§16.3). On review approval, each feedback item is classified, routed to the
real SurgGraph agent it's about, written durably to Firestore (the system
of record), and mirrored into GEAP Memory Bank as a retrievable fact.

The scope shape, fact format, and routing table this module writes with are
NOT owned here — they're a contract shared with the read path four
SurgGraph agents actually consult, and both sides import them from
tools/feedback_kb.py (this project's layering is one-directional: agents/
imports tools/, never the reverse — see that module's own docstring for the
full reasoning). This module re-exports the scope/routing/fact names so
existing call sites (agents/surgbot/root_agent.py, services/surgbot_service/
main.py) can keep saying `feedback.REVIEW_SUMMARY_SCOPE` etc. without caring
which file actually defines them.

ADVISORY ONLY (locked decision, plan_v2 §16.0's table): everything this
module produces is later injected as labelled prompt context. Nothing here
ever gates, suppresses, or auto-tunes a detection threshold. The fail-closed
verification gate (agents/verification_gate/gate.py) is never touched.

INSTITUTION-WIDE BY A CONSTANT SCOPE, NOT A REAL IDENTITY (plan_v2 §16.1c):
SURGGRAPH_KB_USER_ID is a Memory Bank scope value only. It exists so any
reviewer — the author, a judge, anyone — reads and writes the same shared
KB, rather than each starting from an empty one. It is NEVER written as a
Firestore reviewer_id; every FeedbackRecord still carries the real
reviewer_id for provenance. No other multi-tenant boundary in this codebase
(per-case isolation, per-session isolation, the real reviewer_id passed
through session_start) is touched by this constant.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from agents.surgbot import model_armor, store, subagents
from agents.surgbot.schema import CaseReviewDocument, FeedbackKind, FeedbackRecord, ReviewFeedbackItem
from tools import memory_bank
from tools.feedback_kb import (  # noqa: F401 — re-exported, see module docstring
    NODE_TYPE_TO_AGENT,
    REVIEW_SUMMARY_SCOPE,
    SCOPE_ROOT,
    SURGGRAPH_KB_USER_ID,
    FeedbackFact,
    TargetAgent,
    directive_scope,
    format_fact,
    observation_scope,
    parse_fact,
    target_agent_for,
)

logger = logging.getLogger(__name__)


def _fact_body(item: ReviewFeedbackItem) -> str:
    parts = [p for p in (item.rationale, item.coaching_note) if p]
    return " — ".join(parts) if parts else "(no rationale given)"


# --- Classification (§16.3 step 3) ----------------------------------------------


async def _classify(item: ReviewFeedbackItem, node_type: str | None) -> tuple[FeedbackKind, str | None]:
    """Deterministic first, LLM only for the genuine remainder (plan_v2
    §16.3 step 3): a node-anchored item WITH a verdict is always an
    observation, routed by target_agent_for alone — no model call. Anything
    else (no subject node, or a subject node with no verdict) goes to the
    feedback_router subagent, which also assigns target_agent when the item
    has no node to route from at all."""
    if item.subject_node_id and item.verdict is not None:
        return "observation", None  # target_agent already resolved by the caller

    text = _fact_body(item)
    result = await subagents.invoke_subagent(
        "feedback_router",
        f"Feedback text: {text}\nSubject node type (if any): {node_type or 'none'}\nVerdict given (if any): {item.verdict or 'none'}",
    )
    parsed = result.get("parsed")
    if not parsed:
        logger.warning("feedback: feedback_router did not return a parsed classification (%s) — defaulting to observation/unrouted", result.get("error"))
        return "observation", None
    kind = parsed.get("kind", "observation")
    target = parsed.get("target_agent")
    return kind, (target if target and target != "none" else None)


# --- Write path (§16.3) ---------------------------------------------------------


def _synthesis_resource_name() -> str:
    # Duplicated from services/surgbot_service/main.py rather than imported —
    # same reasoning as agents/surgbot/model_armor.py's own duplicated
    # _describe_match: agents/ must not depend on services/, so a 3-line
    # helper is copied rather than the dependency direction inverted.
    engine = subagents.deploy_or_get_subagent("synthesis")
    return engine.api_resource.name


async def process_review_feedback(review: CaseReviewDocument) -> list[FeedbackRecord]:
    """The write path (plan_v2 §16.3): called once, from the review-approval
    hook, on an approved or edited review — unapproved/draft feedback never
    becomes knowledge. For each item: resolve routing, classify kind, Model
    Armor screen, write Firestore (always), write Memory Bank (best-effort,
    only if not blocked and a target_agent was resolved). Returns every
    record produced, including unroutable/blocked ones — callers should not
    assume every input item yields a live memory."""
    records: list[FeedbackRecord] = []
    if not review.feedback_items:
        return records
    # _synthesis_resource_name is a plain blocking call (deploy_or_get_
    # subagent can hit the network) — same shared-event-loop-freezing risk
    # already documented and fixed elsewhere in this module's callers
    # (services/surgbot_service/main.py's get_cases()/approval hook).
    agent_engine = await asyncio.to_thread(_synthesis_resource_name)

    for item in review.feedback_items:
        node_type, target_agent = target_agent_for(item.subject_node_id)
        kind, llm_target = await _classify(item, node_type)
        if target_agent is None:
            target_agent = llm_target

        text = _fact_body(item)
        # Plain blocking Model Armor network call — same to_thread convention
        # root_agent.py::draft_review_document already uses for this exact
        # function.
        screen = await asyncio.to_thread(model_armor.screen_review_document, text)

        record = FeedbackRecord(
            feedback_id=f"fb-{uuid.uuid4().hex[:12]}",
            case_id=item.case_id,
            session_id=review.session_id,
            review_id=review.review_id,
            reviewer_id=review.reviewer_id,  # the REAL reviewer — never the KB constant
            subject_node_id=item.subject_node_id,
            node_type=node_type,
            target_agent=target_agent,
            kind=kind,
            verdict=item.verdict,
            rationale=item.rationale,
            coaching_note=item.coaching_note,
        )

        if screen.blocked:
            record.memory_written = False
            record.memory_error = f"blocked by Model Armor: {screen.reason}"
            logger.warning("feedback[%s]: Model Armor blocked feedback text — not written to Memory Bank (%s)", record.feedback_id, screen.reason)
        elif target_agent is None:
            record.memory_written = False
            record.memory_error = "unroutable — no target_agent resolved"
            logger.info("feedback[%s]: unroutable (subject_node_id=%r) — stored, not indexed", record.feedback_id, item.subject_node_id)
        else:
            fact = format_fact(
                verdict=item.verdict,
                node_type=node_type,
                case_id=item.case_id,
                at=datetime.now(timezone.utc).isoformat(),
                body=text,
            )
            record.fact = fact
            scope = directive_scope(target_agent) if kind == "directive" else observation_scope(target_agent)
            resource = await asyncio.to_thread(memory_bank.create_memory, fact, scope, agent_engine)
            record.memory_written = resource is not None
            record.memory_error = None if resource is not None else "memory_bank.create_memory failed — see logs"
            logger.info(
                "feedback[%s]: routed to %s as %s, memory_written=%s",
                record.feedback_id, target_agent, kind, record.memory_written,
            )

        await store.save_feedback_record(record)
        records.append(record)

    return records
