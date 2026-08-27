"""SurgBot's own Firestore collections — surgbot_reviews/{review_id} and
surgbot_sessions/{session_id} — entirely new, own lazy-singleton
firestore.AsyncClient, independent of (never shared with, never importing
from) services/state_service/store.py. That module owns cases/graph_items;
this one owns nothing there and never touches it.

APPROVAL FOLLOWS agents/hitl/approval.py's PATTERN EXACTLY, not a new
invention: state lives on the artifact itself (approval_status: drafting ->
pending/blocked -> approved/rejected/edited), and the human decision is a
fresh, synchronous write against Firestore — never a parked coroutine, since
a surgeon's approval of a review document may come long after the SurgBot
voice session itself has ended (the surgeon might review the transcript and
decide later, from a different device entirely).

EDIT IS A REAL OUTCOME here too, mirroring the same rationale: a reviewer
correcting the draft's sections before approving is the expected case, and
`original_sections` is preserved alongside the edit so the record shows what
the system produced versus what the reviewer actually signed off on.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from google.cloud.firestore_v1.async_client import AsyncClient

from agents.surgbot.schema import CaseReviewDocument, FeedbackRecord, ReviewFeedbackItem, SurgBotSession

logger = logging.getLogger(__name__)

_REVIEWS_COLLECTION = "surgbot_reviews"
_SESSIONS_COLLECTION = "surgbot_sessions"
_FEEDBACK_COLLECTION = "surgbot_feedback"

_async_client: AsyncClient | None = None


def _database_name() -> str:
    # Same env var/default convention as services/state_service/store.py's
    # _database_name() — same underlying Firestore database, own client.
    return os.environ.get("FIRESTORE_DATABASE", "(default)")


def _get_async_client() -> AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = AsyncClient(database=_database_name())
    return _async_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Sessions ----------------------------------------------------------------


async def create_session(session: SurgBotSession) -> None:
    client = _get_async_client()
    await client.collection(_SESSIONS_COLLECTION).document(session.session_id).set(
        session.model_dump(mode="json")
    )


async def get_session(session_id: str) -> SurgBotSession | None:
    client = _get_async_client()
    doc = await client.collection(_SESSIONS_COLLECTION).document(session_id).get()
    if not doc.exists:
        return None
    return SurgBotSession.model_validate(doc.to_dict())


async def update_session(session_id: str, **fields) -> None:
    """Partial update — phase transitions, review_id linkage, etc. Merges
    rather than overwriting so two concurrent field writes never clobber
    each other."""
    client = _get_async_client()
    await client.collection(_SESSIONS_COLLECTION).document(session_id).set(
        {**fields, "updated_at": _now_iso()}, merge=True
    )


async def append_session_feedback(session_id: str, feedback: ReviewFeedbackItem) -> None:
    """Appends one piece of surgeon feedback to the session's running list —
    root_agent.py's record_feedback tool call site. Read-modify-write rather
    than Firestore's ArrayUnion so feedback items (which can repeat
    structurally, e.g. two "agree" verdicts on different subjects with the
    same phase) are never silently deduped."""
    client = _get_async_client()
    doc_ref = client.collection(_SESSIONS_COLLECTION).document(session_id)
    doc = await doc_ref.get()
    existing = (doc.to_dict() or {}).get("feedback_items", [])
    existing.append(feedback.model_dump(mode="json"))
    await doc_ref.set({"feedback_items": existing, "updated_at": _now_iso()}, merge=True)


async def get_session_feedback(session_id: str) -> list[ReviewFeedbackItem]:
    client = _get_async_client()
    doc = await client.collection(_SESSIONS_COLLECTION).document(session_id).get()
    if not doc.exists:
        return []
    raw = (doc.to_dict() or {}).get("feedback_items", [])
    return [ReviewFeedbackItem.model_validate(item) for item in raw]


# --- Review documents ---------------------------------------------------------


async def save_review_draft(document: CaseReviewDocument) -> None:
    """Writes (or overwrites) the review document — Phase 5's draft, always
    written with approval_status already set by the caller (drafting/pending/
    blocked) before this is called."""
    client = _get_async_client()
    document = document.model_copy(update={"updated_at": datetime.now(timezone.utc)})
    await client.collection(_REVIEWS_COLLECTION).document(document.review_id).set(
        document.model_dump(mode="json")
    )


async def get_review(review_id: str) -> CaseReviewDocument | None:
    client = _get_async_client()
    doc = await client.collection(_REVIEWS_COLLECTION).document(review_id).get()
    if not doc.exists:
        return None
    return CaseReviewDocument.model_validate(doc.to_dict())


class ReviewNotFound(Exception):
    pass


async def record_review_approval(
    review_id: str, outcome: str, edited_sections: dict | None = None
) -> dict:
    """The surgeon's decision on a drafted review document — approve, reject,
    or edit-and-approve. Same shape as agents/hitl/approval.py::
    record_approval: reads the current artifact, computes the new state, and
    writes it back synchronously in one call. There is no FHIR write or
    verification gate on this path (a SurgBot review document is an internal
    coaching/QA artifact, not a clinical record destined for an external
    system) — approval here just finalizes the document's own status.
    """
    document = await get_review(review_id)
    if document is None:
        raise ReviewNotFound(f"no SurgBot review document {review_id!r}")

    sections = dict(document.sections)
    original_sections = document.original_sections

    if outcome == "edited":
        if not edited_sections:
            raise ValueError("outcome 'edited' requires edited_sections")
        original_sections = sections
        sections = {**sections, **edited_sections}

    approved = outcome in ("approved", "edited")
    status = "approved" if approved else "rejected"

    updated = document.model_copy(
        update={
            "approval_status": status,
            "sections": sections,
            "original_sections": original_sections,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    await save_review_draft(updated)
    logger.info("surgbot store: review %s %s", review_id, outcome)
    return {"review_id": review_id, "outcome": outcome, "approval_status": status}


# --- Feedback knowledge base (plan_v2 §16) -------------------------------------
#
# Firestore is the system of record for feedback; agents/surgbot/feedback.py
# writes the same fact to Memory Bank as the retrievable index. One doc per
# feedback item (not an inline array like sessions' feedback_items) because
# this collection needs to be queryable on its own — by target_agent, by
# case, for a future review UI — independent of any one session or review.


async def save_feedback_record(record: FeedbackRecord) -> None:
    client = _get_async_client()
    await client.collection(_FEEDBACK_COLLECTION).document(record.feedback_id).set(
        record.model_dump(mode="json")
    )


async def get_feedback_record(feedback_id: str) -> FeedbackRecord | None:
    client = _get_async_client()
    doc = await client.collection(_FEEDBACK_COLLECTION).document(feedback_id).get()
    if not doc.exists:
        return None
    return FeedbackRecord.model_validate(doc.to_dict())


async def list_feedback_for_review(review_id: str) -> list[FeedbackRecord]:
    """Used by tests/verification scripts to read back what a given review's
    approval actually wrote — not on any hot path."""
    client = _get_async_client()
    query = client.collection(_FEEDBACK_COLLECTION).where("review_id", "==", review_id)
    docs = [doc async for doc in query.stream()]
    return [FeedbackRecord.model_validate(doc.to_dict()) for doc in docs]
