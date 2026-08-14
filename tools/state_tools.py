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

from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
RUNTIME_DIR = DATA_ROOT / "runtime"

_seq_counters: dict[str, itertools.count] = {}


def _next_seq(case_id: str) -> int:
    counter = _seq_counters.setdefault(case_id, itertools.count(1))
    return next(counter)


def _local_fallback_path(case_id: str) -> Path:
    return RUNTIME_DIR / f"{case_id}_graph_patches.jsonl"


def _post_patch_sync(state_service_url: str, case_id: str, event: StateDiffEvent) -> StateDiffEvent:
    resp = requests.post(f"{state_service_url}/state/{case_id}/patch", json=event.model_dump(mode="json"), timeout=10)
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
