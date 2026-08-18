"""Firestore-backed Living State Graph store — genuinely multi-instance-safe.

Replaces an earlier in-memory placeholder (one process, one dict per
case_id). That was an explicit, disclosed stand-in — its own docstring said
so — dropped once the project needed real concurrent-user isolation: every
user gets their own fully independent case, and Cloud Run's max-instances=1
does NOT actually guarantee a single instance (confirmed against Google's
own docs: deploys/traffic spikes can exceed it, scale-to-zero wipes
in-memory state), so in-memory state can't be relied on for anything beyond
throwaway local dev. See plan §11 for the full design/validation history.

A "case" is a monitoring SESSION, not a video — it's the unit of isolation
(one case_id = one user's fully independent pipeline run + graph state).
`video_id` is just a field on the case, the source content it's analyzing;
many concurrent cases can (and, for this project's single demo video, will)
all reference the same video_id while staying completely independent of
each other. Do not conflate the two.

Schema:
  cases/{case_id}                        {case_id, video_id, seq, created_at}
  cases/{case_id}/graph_items/{item_id}  one doc per node OR edge, "kind"
                                          discriminates. item_id =
                                          f"node:{node_id}" or f"edge:{edge_id}".
One subcollection (not separate nodes/edges) means one Firestore real-time
listener per case, not two.

Real-time fan-out uses Firestore's native `on_snapshot` listeners (see
subscribe() below), not a custom Redis/Pub-Sub layer — Firestore already IS
the multi-instance-safe real-time layer for "many concurrent readers,
durable + live" (confirmed via Google's real-time-queries-at-scale docs).
Each SSE connection gets its own listener (not a shared per-case fan-out) —
a deliberate choice given actual expected load is many different concurrent
*cases*, not many browser tabs on the same case; a shared version would need
a threading.Lock (the Watch callback runs on a background OS thread, not
the event loop), not worth the complexity here.

`on_snapshot`'s callback runs on a background thread (confirmed: Watch is
not supported on Firestore's async surface) — bridged into the asyncio
event loop via `loop.call_soon_threadsafe`.

The listener query is scoped to `where(seq > baseline_seq)` (baseline
captured before attaching) specifically so a new subscriber does NOT get
Firestore's initial full-collection "added" snapshot replayed as if it were
live traffic — confirmed via Google's own docs that the first on_snapshot
callback contains "added" events for every existing matching document.

`remove_edge` is never a physical Firestore delete: a deleted document's
snapshot has no data to reconstruct a StateDiffEvent from once the watch
fires. It's a `.set()` overwrite of the same edge doc carrying
`op="remove_edge"`; snapshot() filters those out of the reconstructed edge
list.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.async_client import AsyncClient
from google.cloud.firestore_v1.async_transaction import AsyncTransaction
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.watch import Watch

from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent, StateSnapshot


class TransactionContentionError(Exception):
    """A patch write's Firestore transaction exhausted its retries (default
    max_attempts=5) — real but rare at this project's actual write volume
    (contention only occurs when two writers hit the SAME case doc in the
    same instant). Callers should treat this as retryable, not fatal."""

    def __init__(self, case_id: str) -> None:
        super().__init__(f"transaction contention writing to case {case_id!r} — retries exhausted")
        self.case_id = case_id


def _database_name() -> str:
    return os.environ.get("FIRESTORE_DATABASE", "(default)")


_async_client: AsyncClient | None = None
_sync_client: firestore.Client | None = None


def _get_async_client() -> AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = AsyncClient(database=_database_name())
    return _async_client


def _get_sync_client() -> firestore.Client:
    global _sync_client
    if _sync_client is None:
        _sync_client = firestore.Client(database=_database_name())
    return _sync_client


def _item_id(event: StateDiffEvent) -> str:
    if event.node is not None:
        return f"node:{event.node.node_id}"
    assert event.edge is not None
    return f"edge:{event.edge.edge_id}"


def _event_to_item_body(event: StateDiffEvent) -> dict[str, Any]:
    return {
        "kind": "node" if event.node is not None else "edge",
        "event_id": event.event_id,
        "op": event.op,
        "reason": event.reason,
        "source_agent": event.source_agent,
        "source_tool": event.source_tool,
        "timestamp": event.timestamp.isoformat(),
        "node": event.node.model_dump(mode="json") if event.node is not None else None,
        "edge": event.edge.model_dump(mode="json") if event.edge is not None else None,
    }


def _item_body_to_event(case_id: str, body: dict[str, Any]) -> StateDiffEvent:
    return StateDiffEvent(
        event_id=body["event_id"],
        case_id=case_id,
        seq=body["seq"],
        op=body["op"],
        node=GraphNodePatch.model_validate(body["node"]) if body.get("node") else None,
        edge=GraphEdgePatch.model_validate(body["edge"]) if body.get("edge") else None,
        reason=body["reason"],
        source_agent=body["source_agent"],
        source_tool=body["source_tool"],
        timestamp=datetime.fromisoformat(body["timestamp"]),
    )


def _seq_of(snap: Any) -> int:
    """`DocumentSnapshot.get("seq")` raises KeyError for a field that isn't
    present — a field-PATH getter, not a permissive dict .get() — which a
    real case doc hits whenever it was created without a seq field yet
    (services/orchestrator_service/main.py's initial case_id/video_id/
    created_at write, before any real graph patch has landed). `to_dict()`
    returns None for a nonexistent doc, {} of fields for an existing one —
    either way, a plain dict .get() with a default is safe."""
    return (snap.to_dict() or {}).get("seq", 0)


@firestore.async_transactional
async def _apply_patch_txn(
    transaction: AsyncTransaction, case_ref: Any, item_ref: Any, item_body: dict[str, Any]
) -> int:
    # Firestore transactions require every read before any write.
    case_snap = await case_ref.get(transaction=transaction)
    new_seq = _seq_of(case_snap) + 1
    transaction.set(case_ref, {"seq": new_seq}, merge=True)
    transaction.set(item_ref, {**item_body, "seq": new_seq})
    return new_seq


@firestore.async_transactional
async def _apply_batch_txn(
    transaction: AsyncTransaction, case_ref: Any, items: list[tuple[Any, dict[str, Any]]]
) -> list[int]:
    """Applies N graph items in ONE transaction, assigning consecutive seqs.

    Real measured reason this exists: every write transactionally bumps a
    single `seq` field on the case document, so writes to one case serialize at
    roughly one per second, and eight CONCURRENT writes exhausted the
    transaction's five attempts and returned 503. Error Detection alone writes
    five to ten items per window on a five-second cadence, so per-item
    transactions cannot keep up. Batching turns a window's worth of writes into
    one round trip and one contention point rather than N of each.
    """
    case_snap = await case_ref.get(transaction=transaction)
    base_seq = _seq_of(case_snap)
    seqs = []
    for offset, (item_ref, item_body) in enumerate(items, start=1):
        seq = base_seq + offset
        transaction.set(item_ref, {**item_body, "seq": seq})
        seqs.append(seq)
    transaction.set(case_ref, {"seq": base_seq + len(items)}, merge=True)
    return seqs


class CaseGraphStore:
    """Interface-compatible with the in-memory version it replaces
    (snapshot/apply_patch/subscribe/unsubscribe) — see module docstring.
    `subscribe()`'s return shape is the one necessary difference: a Firestore
    Watch handle, not a bare queue reference, must be kept to unsubscribe."""

    async def snapshot(self, case_id: str) -> StateSnapshot:
        client = _get_async_client()
        case_ref = client.collection("cases").document(case_id)
        case_snap = await case_ref.get()
        seq = _seq_of(case_snap)

        nodes: list[GraphNodePatch] = []
        edges: list[GraphEdgePatch] = []
        async for doc in case_ref.collection("graph_items").stream():
            body = doc.to_dict()
            if body is None:
                continue
            if body["kind"] == "node":
                nodes.append(GraphNodePatch.model_validate(body["node"]))
            elif body["op"] != "remove_edge":
                edges.append(GraphEdgePatch.model_validate(body["edge"]))

        return StateSnapshot(case_id=case_id, seq=seq, nodes=nodes, edges=edges)

    async def apply_patch(self, case_id: str, incoming: StateDiffEvent) -> StateDiffEvent:
        client = _get_async_client()
        case_ref = client.collection("cases").document(case_id)
        item_ref = case_ref.collection("graph_items").document(_item_id(incoming))
        item_body = _event_to_item_body(incoming)

        transaction = client.transaction()
        try:
            new_seq = await _apply_patch_txn(transaction, case_ref, item_ref, item_body)
        except ValueError as exc:
            raise TransactionContentionError(case_id) from exc

        return incoming.model_copy(update={"case_id": case_id, "seq": new_seq})

    async def apply_patch_batch(self, case_id: str, incoming: list[StateDiffEvent]) -> list[StateDiffEvent]:
        """Batch form of apply_patch. Order is preserved: item i gets seq
        base+i+1, so the SSE stream and any seq-based filtering see exactly the
        order the caller intended."""
        if not incoming:
            return []
        client = _get_async_client()
        case_ref = client.collection("cases").document(case_id)
        items = [
            (case_ref.collection("graph_items").document(_item_id(event)), _event_to_item_body(event))
            for event in incoming
        ]

        transaction = client.transaction()
        try:
            seqs = await _apply_batch_txn(transaction, case_ref, items)
        except ValueError as exc:
            raise TransactionContentionError(case_id) from exc

        return [e.model_copy(update={"case_id": case_id, "seq": seq}) for e, seq in zip(incoming, seqs)]

    async def subscribe(self, case_id: str, since_seq: int | None = None) -> tuple[asyncio.Queue[StateDiffEvent], Watch]:
        """Streams every graph item written after `since_seq`.

        `since_seq` MUST be supplied by any client that also fetched a
        snapshot, and must be that snapshot's own seq. Without it this falls
        back to the case's seq as of right now, which opens a real, observed
        data-loss window: a client does GET /snapshot (seq N) and then attaches
        the stream a moment later (seq M), and everything written in (N, M] is
        in neither — not in the snapshot, because it did not exist yet, and not
        in the stream, because the baseline had already moved past it.

        That window is not theoretical. The orchestrator writes the entire
        case skeleton — the trigger node, the patient twin, every agent node
        including the error-detection aggregation step, and their edges — in
        one batch immediately after /cases/open returns, which is exactly when
        a freshly-loaded UI is between its snapshot fetch and its stream
        attach. Losing that batch makes the trigger node absent and leaves
        every error node visually orphaned, because the edges pointing at the
        missing aggregation node get dropped as dangling. Both were reported
        as intermittent UI bugs and both trace here.

        Passing the snapshot's seq closes the gap by construction: the snapshot
        covers everything up to N, the stream covers everything after N, and
        the two meet exactly with no overlap and no hole.
        """
        if since_seq is None:
            client = _get_async_client()
            case_ref = client.collection("cases").document(case_id)
            case_snap = await case_ref.get()
            baseline_seq = _seq_of(case_snap)
        else:
            baseline_seq = since_seq

        queue: asyncio.Queue[StateDiffEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_snapshot(col_snapshot: Any, changes: Any, read_time: Any) -> None:
            for change in changes:
                if change.type.name not in ("ADDED", "MODIFIED"):
                    continue
                body = change.document.to_dict()
                if body is None:
                    continue
                event = _item_body_to_event(case_id, body)
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:
                    pass  # loop torn down mid-flight; listener is being closed anyway

        sync_query = (
            _get_sync_client()
            .collection("cases")
            .document(case_id)
            .collection("graph_items")
            .where(filter=FieldFilter("seq", ">", baseline_seq))
        )
        watch = sync_query.on_snapshot(on_snapshot)
        return queue, watch

    async def unsubscribe(self, case_id: str, watch: Watch) -> None:
        watch.unsubscribe()


store = CaseGraphStore()


# --- Perception audit log (docs/plan_v2 §7.9) -------------------------------
# A SEPARATE subcollection from graph_items, on purpose. This holds every raw
# per-window perception output verbatim, including the redundant ones the
# change-diff layer correctly declined to promote to events. It is never
# rendered on the graph, never streamed to the UI, and never read in a live
# decision path — it exists for post-hoc analysis, benchmark alignment, and
# debugging the debounce rules against what the model actually said.
#
# Keeping it out of graph_items is what makes the separation real rather than
# stated: the SSE stream and every snapshot() call read graph_items only, so
# audit volume cannot leak into the reasoning surface no matter how large it
# grows.


async def write_perception_audit(case_id: str, window_index: int, record: dict[str, Any]) -> None:
    """One document per window, keyed by window_index so a retried window
    overwrites rather than duplicating."""
    client = _get_async_client()
    doc_ref = (
        client.collection("cases")
        .document(case_id)
        .collection("perception_raw")
        .document(str(window_index))
    )
    await doc_ref.set(record)
