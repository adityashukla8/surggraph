"""`apply_state_patch` — the thin write path every agent uses to push a graph
change, per the project's "state is queried/written via tools, not carried
in prompt context" principle (initial_11082026.md §9).

services/state_service (the real FastAPI SSE-backed service) doesn't exist
yet. Rather than block every agent's build on it, this POSTs to it when
`STATE_SERVICE_URL` is set, and otherwise falls back to appending a
StateDiffEvent-shaped line to a local file — zero rework once the real
service exists; just set the env var. See plan §3.5/§9 (Monitor Agent's
build was explicitly designed not to block on state_service being built
first).
"""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path

import requests

from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent, StateSnapshot

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
RUNTIME_DIR = DATA_ROOT / "runtime"

_seq_counters: dict[str, itertools.count] = {}


def _next_seq(case_id: str) -> int:
    counter = _seq_counters.setdefault(case_id, itertools.count(1))
    return next(counter)


def _local_fallback_path(case_id: str) -> Path:
    return RUNTIME_DIR / f"{case_id}_graph_patches.jsonl"


_HTTP_TIMEOUT_S = 30
# Real finding (Anticipation Agent's end-to-end test, once it became a
# third concurrent writer alongside Monitor's own 21-call/window fan-out):
# every write to the same case_id serializes through one Firestore
# document's transaction (the real, single-document `seq` counter,
# services/state_service/store.py) — under real contention, that
# transaction's own 5-attempt retry/backoff can legitimately take longer
# than a tight client timeout, producing a client-side ReadTimeout even
# though the server would have succeeded given more time. 10s was too
# tight once three agents write concurrently; not a symptom of a stuck
# server (confirmed idle-latency stayed sub-100ms in the same session).


def _post_patch_sync(state_service_url: str, case_id: str, event: StateDiffEvent) -> StateDiffEvent:
    resp = requests.post(f"{state_service_url}/state/{case_id}/patch", json=event.model_dump(mode="json"), timeout=_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    # The server's response is authoritative (real seq assigned there) —
    # returning our own pre-request copy would silently misreport it.
    return StateDiffEvent.model_validate(resp.json())


def _write_local_fallback_sync(case_id: str, event: StateDiffEvent) -> StateDiffEvent:
    event = event.model_copy(update={"seq": _next_seq(case_id)})
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with open(_local_fallback_path(case_id), "a") as f:
        f.write(event.model_dump_json() + "\n")
    return event


async def apply_state_patch(
    case_id: str,
    node: GraphNodePatch | None = None,
    edge: GraphEdgePatch | None = None,
    reason: str = "",
    source_agent: str = "",
    source_tool: str = "",
) -> StateDiffEvent:
    """`async def` so this can be awaited directly from a shared, long-lived
    event loop (Orchestrator, the coordinator's real-time on_window_complete
    callback) without blocking other concurrent work on that loop — the
    actual network/file I/O is still the synchronous `requests`/`open()`
    calls underneath (no async HTTP client needed for this call volume), so
    it's offloaded via asyncio.to_thread rather than blocking the caller's
    loop directly."""
    if node is None and edge is None:
        raise ValueError("apply_state_patch requires a node or an edge")

    op = "add_node" if node is not None else "add_edge"
    state_service_url = os.environ.get("STATE_SERVICE_URL")

    # `seq` here is a placeholder, not a claim: the real state service is the
    # sole authority for ordering (services/state_service/store.py always
    # overwrites it) — sending a locally-incremented value that looks
    # meaningful but gets discarded would be its own small dishonesty. Only
    # the local-file fallback path (no server, single writer) treats this
    # counter as real.
    event = StateDiffEvent(
        case_id=case_id,
        seq=0,
        op=op,
        node=node,
        edge=edge,
        reason=reason,
        source_agent=source_agent or (node.source_agent if node else edge.source_agent),
        source_tool=source_tool or (node.source_tool if node else edge.source_tool),
    )

    if state_service_url:
        return await asyncio.to_thread(_post_patch_sync, state_service_url, case_id, event)

    return await asyncio.to_thread(_write_local_fallback_sync, case_id, event)


# --- Read path — Anticipation Agent's get_state_snapshot() tool needs to see
# what other agents have already written (plan §2's get_recent_window_state
# tool spec, agents/anticipation/agent.py's reconciliation pass) ------------


def _item_key(event: StateDiffEvent) -> str:
    return f"node:{event.node.node_id}" if event.node is not None else f"edge:{event.edge.edge_id}"


def _get_snapshot_sync(state_service_url: str, case_id: str) -> StateSnapshot:
    resp = requests.get(f"{state_service_url}/state/{case_id}/snapshot", timeout=_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return StateSnapshot.model_validate(resp.json())


def _read_local_fallback_snapshot_sync(case_id: str) -> StateSnapshot:
    """Replays the local JSONL fallback into the same last-write-wins shape
    services/state_service/store.py's real Firestore-backed snapshot()
    already produces (dedupe by node_id/edge_id, drop remove_edge'd edges) —
    so a caller gets identical semantics whether or not STATE_SERVICE_URL is
    set."""
    path = _local_fallback_path(case_id)
    if not path.exists():
        return StateSnapshot(case_id=case_id, seq=0, nodes=[], edges=[])
    items: dict[str, StateDiffEvent] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = StateDiffEvent.model_validate_json(line)
            items[_item_key(event)] = event
    nodes = [e.node for e in items.values() if e.node is not None]
    edges = [e.edge for e in items.values() if e.edge is not None and e.op != "remove_edge"]
    seq = max((e.seq for e in items.values()), default=0)
    return StateSnapshot(case_id=case_id, seq=seq, nodes=nodes, edges=edges)


async def get_state_snapshot(case_id: str) -> StateSnapshot:
    """`async def`, same dual-path pattern as apply_state_patch: real state
    service when STATE_SERVICE_URL is set, local JSONL replay otherwise."""
    state_service_url = os.environ.get("STATE_SERVICE_URL")
    if state_service_url:
        return await asyncio.to_thread(_get_snapshot_sync, state_service_url, case_id)
    return await asyncio.to_thread(_read_local_fallback_snapshot_sync, case_id)
