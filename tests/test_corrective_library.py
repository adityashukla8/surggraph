"""Tests for the bounded corrective-action library.

This library is the one place in the system that produces suggestions about
what a surgeon should do, so its constraints are worth asserting rather than
trusting: that every action really is grounded in the OCHRA scaffold it claims
to derive from, that its provenance is stated honestly, and — most importantly
— that an action outside the library genuinely cannot reach the graph.

The prompt asks the model to select by action_id, but a prompt is a request.
`resolve` is the enforcement, and these tests are what say so.
"""

from __future__ import annotations

import json

from agents.corrective_replanning import library
from agents.error_detection.knowledge import ERROR_KNOWLEDGE_LIBRARY, get_error_knowledge_entry


def test_every_error_category_has_corrective_actions():
    """A category with no actions can only ever escalate, which would be a
    silent hole rather than a decision."""
    for category in ERROR_KNOWLEDGE_LIBRARY:
        assert library.actions_for(category), f"{category} has no corrective actions"


def test_actions_have_every_required_field_populated():
    for category in ERROR_KNOWLEDGE_LIBRARY:
        for action in library.actions_for(category):
            assert action.action_id.strip()
            assert action.action.strip()
            assert action.rationale.strip()
            # Without a checkable outcome, divergence detection has nothing to
            # measure the surgeon's actual moves against.
            assert action.verification_check.strip()
            assert action.inverts_indicator.strip()


def test_action_ids_are_unique_across_the_whole_library():
    """Ids key corrective_trajectory node ids, so a collision across categories
    would merge two different proposals onto one node."""
    seen: dict[str, str] = {}
    for category in ERROR_KNOWLEDGE_LIBRARY:
        for action in library.actions_for(category):
            assert action.action_id not in seen, f"{action.action_id} duplicated in {category} and {seen[action.action_id]}"
            seen[action.action_id] = category


def test_each_action_inverts_a_real_ochra_error_indicator():
    """The library's own claim is that actions are the stated inverse of the
    observed deviation. This asserts that claim against the real knowledge
    scaffold rather than taking the JSON's word for it."""
    for category in ERROR_KNOWLEDGE_LIBRARY:
        real_indicators = set(get_error_knowledge_entry(category).error_indicators)
        for action in library.actions_for(category):
            assert action.inverts_indicator in real_indicators, (
                f"{action.action_id} claims to invert an indicator that is not in "
                f"{category}'s real OCHRA error_indicators"
            )


def test_provenance_is_stated_honestly():
    """These are surgical suggestions. The strength of their sourcing is part of
    what a reviewer needs, so it must be present and must not overclaim."""
    prov = library.provenance()
    assert prov["tier"] == 2
    assert prov["not_a_clinical_guideline"] is True
    assert "Not reviewed by a practising surgeon" in prov["review_status"]


# --- The enforcement that makes "selects, never generates" real --------------


def test_resolve_rejects_an_action_id_outside_the_library():
    resolved, rejected = library.resolve("tissue_handling", ["th_reduce_traction", "INVENTED_ACTION"])
    assert [a.action_id for a in resolved] == ["th_reduce_traction"]
    assert rejected == ["INVENTED_ACTION"]


def test_resolve_rejects_a_real_id_from_the_wrong_category():
    """A needle-handling action is a real library entry, but proposing it for a
    suture-handling error is still a mismatch the graph must not carry."""
    resolved, rejected = library.resolve("suture_handling", ["nh_regrasp_at_swage"])
    assert resolved == []
    assert rejected == ["nh_regrasp_at_swage"]


def test_resolve_preserves_the_requested_order():
    ids = ["th_reorient_traction", "th_reduce_traction"]
    resolved, _ = library.resolve("tissue_handling", ids)
    assert [a.action_id for a in resolved] == ids


def test_library_json_is_self_describing():
    """A reviewer who does not read Python should be able to open the file and
    understand what it is and how it is used."""
    with open(library.LIBRARY_PATH) as f:
        raw = json.load(f)
    assert "never generates free-form clinical text" in raw["_disclosure"]
    assert "_schema" in raw
