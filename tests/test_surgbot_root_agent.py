"""The deliberate-exception regression guard for SurgBot's root agent —
contrasts directly with tests/test_anticipation_agent.py::
test_anticipation_agent_has_no_tools, which asserts the OPPOSITE for every
other agent in this codebase. SurgBot's root agent is the one place real
tools=[...] is correct (agents/surgbot/root_agent.py's module docstring
explains why); this test keeps that exception from silently regressing back
to (or drifting away from) the tool set root_agent.py actually needs.
"""

from __future__ import annotations

from agents.surgbot.root_agent import TOOL_DISCLOSURE, TOOL_PHASE_MAP, build_root_agent
from agents.surgbot.schema import ApiSurface


def test_surgbot_root_agent_has_real_tools():
    agent = build_root_agent()
    assert len(agent.tools) > 0
    names = {getattr(t, "__name__", None) for t in agent.tools}
    assert "load_case_graph" in names
    assert "draft_review_document" in names
    assert "list_accessible_cases" in names
    assert "review_error_chain" in names
    assert "review_proposal_divergence" in names
    assert "record_feedback" in names
    assert "retrieve_reviewer_patterns" in names
    assert "get_phase_detail" in names


def test_every_tool_has_a_complete_disclosure_entry():
    """The disclosure requirement (agents/surgbot/schema.py::ToolUseDisclosure)
    is only as real as this table — every tool root_agent.py exposes must
    resolve to a real agent_name + model_id + api_surface triple, never a
    partial one, so services/surgbot_service/main.py can never emit a
    disclosure chip with one half missing."""
    agent = build_root_agent()
    tool_names = {getattr(t, "__name__") for t in agent.tools}
    for name in tool_names:
        assert name in TOOL_DISCLOSURE, f"tool {name!r} has no TOOL_DISCLOSURE entry"
        entry = TOOL_DISCLOSURE[name]
        assert entry["agent_name"]
        assert entry["model_id"]
        assert entry["api_surface"] in ApiSurface.__args__


def test_phase_map_only_references_real_review_phases():
    from agents.surgbot.schema import ReviewPhase

    valid_phases = set(ReviewPhase.__args__)
    for tool_name, phase in TOOL_PHASE_MAP.items():
        assert phase in valid_phases, f"{tool_name} maps to invalid phase {phase}"


def test_subagent_dispatching_tools_disclose_gemini_35_not_live_model():
    """The specific, hard product requirement: a tool that dispatches to a
    deployed Gemini-3.5 subagent must disclose THAT model, not the Live
    model the root agent's own voice runs on — this is exactly the
    distinction the disclosure banner exists to make legible."""
    for tool_name in ("review_error_chain", "draft_review_document", "retrieve_reviewer_patterns"):
        entry = TOOL_DISCLOSURE[tool_name]
        assert entry["api_surface"] == "vertex_ai_global"
        assert "live" not in entry["model_id"].lower()
