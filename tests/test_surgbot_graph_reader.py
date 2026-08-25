"""agents/surgbot/graph_reader.py — thin wrapper on tools/context_slice.py::
build_index, plus the one genuinely new bit (get_case_indexes's concurrent
fan-out). Same local-fallback round-trip pattern already used in
tests/test_anticipation_agent.py::test_get_state_snapshot_local_fallback_
round_trip — no STATE_SERVICE_URL means tools/state_tools.py replays the
local JSONL fallback, which is exactly what these tests exercise.
"""

from __future__ import annotations

import asyncio
import uuid

from agents.surgbot.graph_reader import build_index, get_case_indexes
from state.schema import GraphNodePatch
from tools.state_tools import apply_state_patch


def test_get_case_index_local_fallback_round_trip(monkeypatch):
    monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
    case_id = f"test-surgbot-case-{uuid.uuid4().hex[:8]}"

    async def run():
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id="phase:1", node_type="phase", label="Phase 1", source_agent="test", source_tool="test"
            ),
            reason="first write",
        )
        return await build_index(case_id)

    index = asyncio.run(run())
    assert index.snapshot.case_id == case_id
    phases = index.of_type("phase")
    assert len(phases) == 1
    assert phases[0].label == "Phase 1"


def test_get_case_indexes_concurrent_fan_out(monkeypatch):
    monkeypatch.delenv("STATE_SERVICE_URL", raising=False)
    case_a = f"test-surgbot-case-{uuid.uuid4().hex[:8]}"
    case_b = f"test-surgbot-case-{uuid.uuid4().hex[:8]}"

    async def run():
        await apply_state_patch(
            case_a,
            node=GraphNodePatch(node_id="phase:1", node_type="phase", label="Case A phase", source_agent="t", source_tool="t"),
            reason="a",
        )
        await apply_state_patch(
            case_b,
            node=GraphNodePatch(node_id="phase:1", node_type="phase", label="Case B phase", source_agent="t", source_tool="t"),
            reason="b",
        )
        return await get_case_indexes([case_a, case_b])

    indexes = asyncio.run(run())
    assert set(indexes.keys()) == {case_a, case_b}
    assert indexes[case_a].of_type("phase")[0].label == "Case A phase"
    assert indexes[case_b].of_type("phase")[0].label == "Case B phase"


def test_get_case_indexes_empty_list_returns_empty_dict():
    result = asyncio.run(get_case_indexes([]))
    assert result == {}
