"""Tests for the Firestore-backed Living State Graph store (plan §11).

These hit a real, disposable Firestore database (`surggraph-test`, Native
mode, us-central1) — not a mock, not the emulator — matching this project's
existing testing philosophy (test_fhir_write_readback.py's own docstring:
"these tests hit the real ... server ... no mocking of the call itself,
that's the point"). Every case created here is cleaned up (recursive
delete) after its test.

Covers the specific bugs an independent review pass caught and fixed
before this store shipped (see services/state_service/store.py's module
docstring): concurrent-case isolation (the actual point of the Firestore
rewrite — every user's case must be fully independent), a new subscriber
must never be replayed a case's history as if it were live traffic, a
listener must not go on delivering after unsubscribe, and remove_edge must
never physically delete (which would lose the payload the SSE stream needs)."""

from __future__ import annotations

import asyncio
import os
import uuid

os.environ.setdefault("FIRESTORE_DATABASE", "surggraph-test")

import pytest

from services.state_service import store as store_module
from services.state_service.store import CaseGraphStore, _get_sync_client
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent


@pytest.fixture(autouse=True)
def _fresh_firestore_clients():
    """store.py's Firestore clients are lazy module-level singletons — the
    right pattern for a real server process (one event loop for its whole
    lifetime). pytest-asyncio gives each test function its own event loop
    by default, and a gRPC async channel is bound to the loop it was
    created on — reusing a client cached from an earlier test's (now
    closed) loop surfaces as confusing failures (observed: spurious
    TransactionContentionError on a brand-new, uncontended case). Reset
    the singletons before every test so each gets a fresh client bound to
    that test's actual current loop; this is a test-harness concern only,
    not something production code needs to guard against."""
    store_module._async_client = None
    store_module._sync_client = None
    yield
    store_module._async_client = None
    store_module._sync_client = None


def _case_id(label: str) -> str:
    return f"test-{label}-{uuid.uuid4().hex[:10]}"


def _node_event(case_id: str, node_id: str, reason: str = "") -> StateDiffEvent:
    return StateDiffEvent(
        case_id=case_id,
        seq=0,
        op="add_node",
        node=GraphNodePatch(
            node_id=node_id,
            node_type="agent",
            label=node_id,
            source_agent="test_suite",
            source_tool="test_state_service_store",
        ),
        reason=reason,
        source_agent="test_suite",
        source_tool="test_state_service_store",
    )


def _edge_event(case_id: str, edge_id: str, op: str = "add_edge") -> StateDiffEvent:
    return StateDiffEvent(
        case_id=case_id,
        seq=0,
        op=op,
        edge=GraphEdgePatch(
            edge_id=edge_id,
            source_node_id="agent:a",
            target_node_id="agent:b",
            edge_kind="hierarchy",
            source_agent="test_suite",
            source_tool="test_state_service_store",
        ),
        reason="",
        source_agent="test_suite",
        source_tool="test_state_service_store",
    )


@pytest.fixture
def store() -> CaseGraphStore:
    return CaseGraphStore()


@pytest.fixture
def cleanup_cases():
    """Tracks case_ids created during a test and recursively deletes them
    (case doc + graph_items subcollection) from the real test database
    afterward — this is real, disposable infra, not left to accumulate."""
    created: list[str] = []
    yield created
    client = _get_sync_client()
    for case_id in created:
        client.recursive_delete(client.collection("cases").document(case_id))


@pytest.mark.asyncio
async def test_concurrent_case_isolation(store: CaseGraphStore, cleanup_cases: list[str]):
    case_a = _case_id("iso-a")
    case_b = _case_id("iso-b")
    cleanup_cases.extend([case_a, case_b])

    async def write_n(case_id: str, n: int) -> None:
        for i in range(n):
            await store.apply_patch(case_id, _node_event(case_id, f"agent:n{i}"))

    await asyncio.gather(write_n(case_a, 8), write_n(case_b, 8))

    snap_a = await store.snapshot(case_a)
    snap_b = await store.snapshot(case_b)

    assert snap_a.seq == 8
    assert snap_b.seq == 8
    assert {n.node_id for n in snap_a.nodes} == {f"agent:n{i}" for i in range(8)}
    assert {n.node_id for n in snap_b.nodes} == {f"agent:n{i}" for i in range(8)}


@pytest.mark.asyncio
async def test_mid_sequence_subscribe_gets_no_replay(store: CaseGraphStore, cleanup_cases: list[str]):
    case_id = _case_id("noreplay")
    cleanup_cases.append(case_id)

    await store.apply_patch(case_id, _node_event(case_id, "agent:before"))

    queue, watch = await store.subscribe(case_id)
    try:
        await store.apply_patch(case_id, _node_event(case_id, "agent:after"))

        event = await asyncio.wait_for(queue.get(), timeout=15)
        assert event.node is not None
        assert event.node.node_id == "agent:after"
        assert queue.empty()  # the pre-existing "agent:before" must NOT have been replayed
    finally:
        await store.unsubscribe(case_id, watch)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(store: CaseGraphStore, cleanup_cases: list[str]):
    case_id = _case_id("cleanup")
    cleanup_cases.append(case_id)

    queue, watch = await store.subscribe(case_id)
    await store.unsubscribe(case_id, watch)

    await store.apply_patch(case_id, _node_event(case_id, "agent:after-unsubscribe"))

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=8)


@pytest.mark.asyncio
async def test_remove_edge_is_never_a_physical_delete(store: CaseGraphStore, cleanup_cases: list[str]):
    case_id = _case_id("removeedge")
    cleanup_cases.append(case_id)

    await store.apply_patch(case_id, _edge_event(case_id, "e1", op="add_edge"))
    snap_before = await store.snapshot(case_id)
    assert any(e.edge_id == "e1" for e in snap_before.edges)

    removed = await store.apply_patch(case_id, _edge_event(case_id, "e1", op="remove_edge"))
    assert removed.op == "remove_edge"
    assert removed.edge is not None
    assert removed.edge.edge_id == "e1"  # full payload intact, not lost like a real delete would

    snap_after = await store.snapshot(case_id)
    assert not any(e.edge_id == "e1" for e in snap_after.edges)
