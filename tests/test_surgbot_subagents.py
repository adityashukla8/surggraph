"""Only root_agent.py breaks the no-tools convention. Every SurgBot
subagent (error_chain_reviewer, synthesis, pattern_insight) must keep
following this codebase's normal convention exactly, same as every non-
SurgBot agent (contrast: tests/test_anticipation_agent.py::
test_anticipation_agent_has_no_tools).
"""

from __future__ import annotations

from agents.surgbot.subagents import SUBAGENT_KINDS, _OUTPUT_SCHEMAS, build_subagent


def test_surgbot_reasoning_subagents_still_follow_no_tools_convention():
    for kind in SUBAGENT_KINDS:
        agent = build_subagent(kind)
        assert agent.tools == []
        assert agent.output_schema is _OUTPUT_SCHEMAS[kind]


def test_subagent_kinds_match_plan_14_1_and_16():
    # error_chain_reviewer/synthesis/pattern_insight are plan_v2 §14.1's
    # original three; feedback_router is plan_v2 §16.3's feedback
    # classification subagent — real 4th kind, not a stray addition.
    assert set(SUBAGENT_KINDS) == {"error_chain_reviewer", "synthesis", "pattern_insight", "feedback_router"}
