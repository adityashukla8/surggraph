"""plan_v2 §16.9 Step 1 — feedback.py foundations, offline/fast, no network.

Covers: fact format/parse round-trip, node_type_for-based routing against
real node-id conventions, scope-builder distinctness, and the multi-tenancy
regression guard for SURGGRAPH_KB_USER_ID (plan_v2 §16.1c) — the constant
must never leak into anything but a Memory Bank scope dict.
"""

from __future__ import annotations

import inspect

import agents.surgbot.feedback as feedback
import agents.surgbot.root_agent as root_agent
import agents.surgbot.schema as schema
import agents.surgbot.store as store
import tools.feedback_kb as feedback_kb
import tools.memory_bank as memory_bank
from agents.surgbot.feedback import (
    SURGGRAPH_KB_USER_ID,
    directive_scope,
    format_fact,
    observation_scope,
    parse_fact,
    target_agent_for,
)
from state import node_ids


# --- Fact round-trip -------------------------------------------------------


def test_format_parse_fact_round_trip():
    fact = format_fact(
        verdict="disagree",
        node_type="divergence_alert",
        case_id="case-4efe11877da3",
        at="2026-08-26T15:00:00+00:00",
        body="First divergence was a false positive.",
    )
    parsed = parse_fact(fact)
    assert parsed.verdict == "disagree"
    assert parsed.node_type == "divergence_alert"
    assert parsed.case_id == "case-4efe11877da3"
    assert parsed.at == "2026-08-26T15:00:00+00:00"
    assert parsed.body == "First divergence was a false positive."


def test_parse_fact_missing_fields_round_trip_as_empty_none():
    fact = format_fact(verdict=None, node_type=None, case_id=None, at="", body="prefer literature under 10 years old")
    parsed = parse_fact(fact)
    assert parsed.verdict is None
    assert parsed.node_type is None
    assert parsed.case_id is None
    assert parsed.body == "prefer literature under 10 years old"


def test_parse_fact_malformed_or_headerless_never_raises():
    # A hand-written or legacy memory with no header at all.
    parsed = parse_fact("just some plain text with no header")
    assert parsed.body == "just some plain text with no header"
    assert parsed.verdict is None

    # A header-shaped-but-broken string still parses to something, not a crash.
    parsed2 = parse_fact("[verdict=disagree not closed properly")
    assert parsed2.body == "[verdict=disagree not closed properly"


# --- Routing, against REAL node-id conventions (state/node_ids.py) --------


def test_routing_divergence_alert():
    node_id = node_ids.divergence_alert("corrective:err1:some_plan", 3)
    node_type, target = target_agent_for(node_id)
    assert node_type == "divergence_alert"
    assert target == "divergence_detection"


def test_routing_complication():
    node_id = node_ids.complication("error:5:needle_handling", "Bladder-neck injury")
    node_type, target = target_agent_for(node_id)
    assert node_type == "complication"
    assert target == "complication_reasoning"


def test_routing_corrective_trajectory():
    node_id = node_ids.corrective_trajectory("error:5:needle_handling", "Back out and replan")
    node_type, target = target_agent_for(node_id)
    assert node_type == "corrective_trajectory"
    assert target == "corrective_replanning"


def test_routing_literature_evidence():
    node_id = node_ids.literature_evidence("abcd1234", 0)
    node_type, target = target_agent_for(node_id)
    assert node_type == "literature_evidence"
    assert target == "literature_retrieval"


def test_routing_error_is_captured_but_target_agent_still_resolves():
    # error_detection is deliberately NOT consumed in v1 (plan_v2 §16.7), but
    # routing itself must still resolve — the KB is ready the day that
    # agent's construction changes.
    node_id = node_ids.error(5, "needle_handling")
    node_type, target = target_agent_for(node_id)
    assert node_type == "error"
    assert target == "error_detection"


def test_routing_unknown_convention_returns_none_not_a_guess():
    node_type, target = target_agent_for("totally-not-a-real-node-id")
    assert node_type is None
    assert target is None


def test_routing_empty_subject_node_id_returns_none():
    node_type, target = target_agent_for("")
    assert node_type is None
    assert target is None


def test_routing_structural_node_types_have_no_entry():
    # Sanity check the table doesn't accidentally claim to route something
    # that was never in scope (plan_v2 §16.5's four agents + error_detection).
    for node_type in ("trigger", "agent", "patient_twin", "documentation", "action_outcome"):
        assert node_type not in feedback.NODE_TYPE_TO_AGENT


# --- Scope builders ----------------------------------------------------------


def test_scope_builders_are_distinct_and_carry_the_constant_kb_id():
    d = directive_scope("literature_retrieval")
    o = observation_scope("literature_retrieval")
    assert d != o
    assert d["kind"] == "directive"
    assert o["kind"] == "observation"
    for scope in (d, o, feedback.REVIEW_SUMMARY_SCOPE):
        assert scope["user_id"] == SURGGRAPH_KB_USER_ID
        assert scope["agent_name"] == "surggraph"


def test_scope_builders_route_differently_per_target_agent():
    assert directive_scope("literature_retrieval") != directive_scope("divergence_detection")


# --- Multi-tenancy regression guard (plan_v2 §16.1c) -----------------------


def test_kb_constant_never_leaks_outside_memory_bank_scope_construction():
    """The constant is a Memory Bank scope value ONLY. This is the concrete,
    automatic guard against a future edit quietly swapping it in for a real
    reviewer_id somewhere it must never appear — the exact failure mode the
    user explicitly flagged when reviewing this design."""
    modules_that_must_not_reference_it = [store, schema, root_agent]
    for module in modules_that_must_not_reference_it:
        source = inspect.getsource(module)
        assert "SURGGRAPH_KB_USER_ID" not in source, f"{module.__name__} must not import/reference the constant KB scope id"

    # And the real per-record reviewer_id field must still be a required,
    # plain string with no default tying it to the constant.
    assert schema.FeedbackRecord.model_fields["reviewer_id"].is_required()
    assert schema.SurgBotSession.model_fields["reviewer_id"].is_required()
    assert schema.CaseReviewDocument.model_fields["reviewer_id"].is_required()


def test_tools_modules_never_import_from_agents():
    # This project's layering: agents/ -> tools/, never the reverse
    # (tools/feedback_kb.py's own docstring states this as the reason it,
    # not agents/surgbot/feedback.py, owns the shared scope/fact/routing
    # contract). Real regression guard, not just a comment.
    for module in (feedback_kb, memory_bank):
        source = inspect.getsource(module)
        assert "import agents" not in source and "from agents" not in source, (
            f"{module.__name__} must not import from agents/ — layering violation"
        )


def test_shared_contract_is_reexported_not_duplicated():
    # agents/surgbot/feedback.py must import these, never redefine them —
    # a silent fork here is exactly how a writer/reader contract drifts.
    assert feedback.NODE_TYPE_TO_AGENT is feedback_kb.NODE_TYPE_TO_AGENT
    assert feedback.REVIEW_SUMMARY_SCOPE is feedback_kb.REVIEW_SUMMARY_SCOPE
    assert feedback.directive_scope is feedback_kb.directive_scope
    assert feedback.format_fact is feedback_kb.format_fact
    assert feedback.target_agent_for is feedback_kb.target_agent_for


def test_tools_feedback_kb_is_the_canonical_definer_of_the_constant():
    # agents/surgbot/feedback.py re-exports the constant (import, not
    # redefinition) so existing call sites can keep saying
    # feedback.SURGGRAPH_KB_USER_ID — but tools/feedback_kb.py must be the
    # one real place it's defined, per this module's own layering rule
    # (agents/ -> tools/, never the reverse).
    assert feedback_kb.SURGGRAPH_KB_USER_ID == "1"
    assert feedback.SURGGRAPH_KB_USER_ID is feedback_kb.SURGGRAPH_KB_USER_ID
    assert "SURGGRAPH_KB_USER_ID = " not in inspect.getsource(feedback)
    assert "SURGGRAPH_KB_USER_ID = " in inspect.getsource(feedback_kb)


# --- memory_bank.py: explicit scope, not reviewer_id ------------------------


def test_memory_bank_functions_take_an_explicit_scope_not_a_reviewer_id():
    create_params = list(inspect.signature(memory_bank.create_memory).parameters)
    retrieve_params = list(inspect.signature(memory_bank.retrieve_memories).parameters)
    assert "scope" in create_params
    assert "scope" in retrieve_params
    assert "reviewer_id" not in create_params
    assert "reviewer_id" not in retrieve_params


def test_retrieve_memories_query_is_optional_for_simple_retrieval():
    sig = inspect.signature(memory_bank.retrieve_memories)
    assert sig.parameters["query"].default is None


# --- ReviewFeedbackItem / record_feedback: subject_node_id now optional ----


def test_review_feedback_item_subject_node_id_is_optional():
    item = schema.ReviewFeedbackItem(phase=2, case_id="case-1", rationale="prefer literature under 10 years old")
    assert item.subject_node_id == ""


def test_record_feedback_signature_allows_omitting_subject_node_id():
    sig = inspect.signature(root_agent.record_feedback)
    assert sig.parameters["subject_node_id"].default == ""
