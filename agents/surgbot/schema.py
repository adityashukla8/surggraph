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

ApiSurface = Literal["vertex_ai_global", "vertex_ai_live"]

ApprovalStatus = Literal["drafting", "pending", "blocked", "approved", "rejected", "edited"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ToolUseDisclosure(BaseModel):
    """The hard, non-optional disclosure record for every tool/agent
    invocation SurgBot's root agent makes. agent_name and model_id are
    required fields, never optional, specifically so the UI can never render
    a disclosure chip with one half missing — see plan §14.0's disclosure
    requirement: this project uses a Live API model (below the hackathon's
    "Gemini 3.5+" bar) for voice/turn-taking only, while every actual
    reasoning step runs on real Gemini 3.5 — that distinction must never be
    blurred or hidden, and this schema is the mechanism that enforces it.
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
    subject_node_id: str
    verdict: AgreementVerdict | None = None
    rationale: str = ""
    coaching_note: str = ""
    recorded_at: datetime = Field(default_factory=_now)


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
    live_model_id: str = ""
    running_summary: str = ""  # carried-forward context across a bidi reconnect (§14.4)
    review_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
