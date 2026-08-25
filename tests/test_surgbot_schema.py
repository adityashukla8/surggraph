"""Regression guard for the hard disclosure requirement (plan §14.0):
ToolUseDisclosure.agent_name and .model_id must always be required fields,
never optional — the UI must never be able to render a disclosure chip with
one half missing. Also guards ReviewPhase's deliberate "5 is absent"
numbering and the approval-status vocabulary agents/surgbot/store.py relies
on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.surgbot.schema import (
    ApprovalStatus,
    CaseReviewDocument,
    PHASE_LABELS,
    ReviewFeedbackItem,
    ReviewPhase,
    ToolUseDisclosure,
)


def test_tool_use_disclosure_requires_both_agent_and_model():
    fields = ToolUseDisclosure.model_fields
    assert fields["agent_name"].is_required()
    assert fields["model_id"].is_required()
    assert fields["api_surface"].is_required()


def test_tool_use_disclosure_rejects_missing_model_id():
    with pytest.raises(ValidationError):
        ToolUseDisclosure(call_id="c1", agent_name="surgbot_root", api_surface="vertex_ai_live")


def test_review_phase_is_six_phases_no_gap():
    assert set(ReviewPhase.__args__) == {1, 2, 3, 4, 5, 6}


def test_phase_labels_cover_every_review_phase():
    assert set(PHASE_LABELS.keys()) == set(ReviewPhase.__args__)
    assert all(isinstance(v, str) and v for v in PHASE_LABELS.values())


def test_approval_status_vocabulary_matches_hitl_convention():
    # Mirrors agents/hitl/approval.py::ApprovalOutcome's own status
    # vocabulary, extended with SurgBot's own "drafting"/"blocked" states.
    assert set(ApprovalStatus.__args__) == {"drafting", "pending", "blocked", "approved", "rejected", "edited"}


def test_case_review_document_round_trips_feedback_items():
    item = ReviewFeedbackItem(phase=3, case_id="case-1", subject_node_id="error-1", verdict="agree")
    doc = CaseReviewDocument(
        review_id="rev-1", case_id="case-1", session_id="sess-1", reviewer_id="reviewer@example.com",
        feedback_items=[item],
    )
    dumped = doc.model_dump(mode="json")
    restored = CaseReviewDocument.model_validate(dumped)
    assert restored.feedback_items[0].verdict == "agree"
    assert restored.approval_status == "drafting"
