"""SurgBot's own Pydantic models — review docs, sessions, feedback, disclosure.

Mirrors state/schema.py's "single source of truth, hand-mirrored in the
frontend" convention: ui/frontend/src/surgbot/types.ts hand-mirrors the
wire-facing subset of this file. Keep both in sync when either changes.

This file is entirely new and does not touch state/schema.py — SurgBot's own
review artifacts (CaseReviewDocument, SurgBotSession) are a different kind of
object from the existing Living Graph vocabulary (GraphNodePatch/EdgeKind/
NodeType) and live in their own Firestore collections (agents/surgbot/store.py),
not on the case graph.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# Renumbered to a clean 6-phase script, no gap (root_agent.py's own
# _INSTRUCTION is the source of truth for phase numbering — this must match
# it exactly): 1 framing, 2 phase walkthrough, 3 error/complication review,
# 4 proposal/divergence review, 5 synthesis + approval, 6 cross-session
# pattern review. (Earlier revision had a 7-phase script with 5 skipped —
# superseded, not layered on top of.)
ReviewPhase = Literal[1, 2, 3, 4, 5, 6]

PHASE_LABELS: dict[int, str] = {
    1: "Case framing",
    2: "Phase-by-phase walkthrough",
    3: "Error-and-complication review",
    4: "Proposal-and-divergence review",
    5: "Synthesis and approval",
    6: "Cross-session pattern review",
}

AgreementVerdict = Literal["agree", "disagree", "uncertain"]

ApiSurface = Literal[
    "vertex_ai_global", "google_cloud_speech", "google_cloud_tts", "vertex_ai_medasr", "vertex_ai_model_armor"
]

ApprovalStatus = Literal["drafting", "pending", "blocked", "approved", "rejected", "edited"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ToolUseDisclosure(BaseModel):
    """The hard, non-optional disclosure record for every tool/agent
    invocation SurgBot's root agent makes, AND (plan_v2 §15) for the STT/TTS
    pipeline stages either side of it — agent_name and model_id are required
    fields, never optional, specifically so the UI can never render a
    disclosure chip with one half missing. Every reasoning step runs on real
    Gemini 3.5 (api_surface="vertex_ai_global"); the two speech stages
    disclose their own real service ("google_cloud_speech"/
    "google_cloud_tts") — never hidden, never blurred together.
    """

    call_id: str
    agent_name: str
    model_id: str
    api_surface: ApiSurface
    tool_name: str = ""
    args_summary: str = ""
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    summary: str = ""


class ReviewFeedbackItem(BaseModel):
    phase: ReviewPhase
    case_id: str
    # Optional (plan_v2 §16.3): a case-grounded observation is anchored to a
    # real graph node ("that divergence was a false positive"), but a
    # standing directive is not ("prefer literature under 10 years old") —
    # forcing a node id onto the latter would mean it could never be
    # recorded at all. Empty string, not None, to match every other
    # optional-but-always-present string field on this model.
    subject_node_id: str = ""
    verdict: AgreementVerdict | None = None
    rationale: str = ""
    coaching_note: str = ""
    recorded_at: datetime = Field(default_factory=_now)


# A reviewer's feedback is one of two genuinely different things, and
# conflating them makes the read path wrong (plan_v2 §16.2):
#   "directive"   — a standing preference that always applies, e.g. "prefer
#                   literature under 10 years old". Few; injected in full.
#   "observation" — a case-grounded judgment, e.g. "that divergence was a
#                   false positive". Many; retrieved by similarity to the
#                   situation actually at hand.
# A directive crowded out of a top-k similarity ranking by unrelated
# observations would silently stop applying, which is why these are stored
# under separate Memory Bank scopes rather than one pool.
FeedbackKind = Literal["directive", "observation"]


class FeedbackRecord(BaseModel):
    """The durable, auditable record of one piece of reviewer feedback.

    Firestore (`surgbot_feedback/{feedback_id}`) is the system of record;
    Memory Bank holds the same text as a retrievable fact. That dual-write is
    deliberate, not redundant: Firestore is queryable, joinable back to the
    case, and survives a Memory Bank outage; Memory Bank provides the managed
    semantic retrieval Firestore has no equivalent for.

    `reviewer_id` is the REAL reviewer — deliberately NOT the constant KB
    user_id that agents/surgbot/feedback.py uses for the Memory Bank scope.
    The KB is shared so anyone's review informs the pipeline; "who said this"
    must still be recoverable from the record itself.
    """

    feedback_id: str
    case_id: str
    session_id: str
    review_id: str
    reviewer_id: str
    subject_node_id: str = ""
    node_type: str | None = None
    target_agent: str | None = None  # None = unroutable; stored, never consumed
    kind: FeedbackKind = "observation"
    verdict: AgreementVerdict | None = None
    rationale: str = ""
    coaching_note: str = ""
    fact: str = ""  # the exact string handed to Memory Bank
    # Written to Firestore FIRST and always, so a Memory Bank failure is a
    # visible, replayable state rather than feedback silently lost.
    memory_written: bool = False
    memory_error: str | None = None
    created_at: datetime = Field(default_factory=_now)


class CaseReviewDocument(BaseModel):
    review_id: str
    case_id: str
    session_id: str
    reviewer_id: str
    approval_status: ApprovalStatus = "drafting"
    case_summary: str = ""
    follow_up_items: list[str] = Field(default_factory=list)
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    coaching_notes: list[str] = Field(default_factory=list)
    threshold_adjustments: list[str] = Field(default_factory=list)
    sections: dict[str, Any] = Field(default_factory=dict)  # surgeon-editable
    original_sections: dict[str, Any] | None = None  # preserved on edit, mirrors agents/hitl/approval.py
    model_armor_reason: str | None = None
    feedback_items: list[ReviewFeedbackItem] = Field(default_factory=list)
    tool_uses: list[ToolUseDisclosure] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SurgBotSession(BaseModel):
    session_id: str
    case_ids: list[str]
    reviewer_id: str
    current_phase: ReviewPhase = 1
    reasoning_model_id: str = ""  # provenance: which Gemini model handled this session
    review_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
