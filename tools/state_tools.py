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
import logging
import os
from pathlib import Path

import httpx
import requests

from state import event_bus
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent, StateSnapshot

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
RUNTIME_DIR = DATA_ROOT / "runtime"

_seq_counters: dict[str, itertools.count] = {}


def _next_seq(case_id: str) -> int:
    counter = _seq_counters.setdefault(case_id, itertools.count(1))
    return next(counter)


def _local_fallback_path(case_id: str) -> Path:
    return RUNTIME_DIR / f"{case_id}_graph_patches.jsonl"


_HTTP_TIMEOUT_S = 30

# One shared async client, lazily created on the running loop.
#
# Real measured problem this fixes: writes went out via `requests.post` wrapped
# in `asyncio.to_thread`, which uses Python's DEFAULT thread pool — the same
# pool the Gemini SDK's blocking I/O draws from. Once dozens of concurrent
# Gemini calls are in flight, a cheap graph write queues behind them and can be
# delayed by minutes; that is the documented cause of the static hierarchy
# arriving ~3 minutes late, and why _draw_static_hierarchy exists at all. An
# async HTTP client never touches the thread pool, so writes stay fast no
# matter how saturated it gets.
#
# Keyed by event loop, not a bare module global: httpx clients bind to the loop
# that created them, and this module is imported by tests and scripts that each
# run their own asyncio.run().
_async_clients: dict[object, httpx.AsyncClient] = {}


# Uvicorn closes an idle keep-alive connection after 5s by default, while this
# system's writers can legitimately go far longer than that between calls — an
# Error Detection window is 15s+ of Gemini latency before its next write. httpx
# pools and reuses connections, so it would hand back a socket the server had
# already closed, and the next request died with a bare ReadError. That is
# exactly what killed the Error Detection sweep on its first window: no error
# node was ever written, so nothing downstream of it could fire either.
#
# `requests` never hit this because, used without a Session, it opens a fresh
# connection per call — the pooling that makes httpx faster is what exposed it.
#
# Expire pooled connections comfortably before the server does. Reuse still
# works for the rapid-fire batched writes that actually benefit from it.
_KEEPALIVE_EXPIRY_S = 3.0

# Transport-level errors are connection failures, not application failures: the
# request never reached the server, so retrying it is safe and cannot duplicate
# a write.
_TRANSPORT_RETRIES = 3


def _get_async_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _async_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S,
            limits=httpx.Limits(keepalive_expiry=_KEEPALIVE_EXPIRY_S),
            transport=httpx.AsyncHTTPTransport(retries=_TRANSPORT_RETRIES),
        )
        _async_clients[loop] = client
    return client


async def _request_with_retry(send, what: str):
    """Retries a request that failed at the transport layer.

    A stale pooled connection is the common case and succeeds immediately on
    the retry with a fresh one. Kept separate from the 503 contention retry:
    that one is the server saying "busy, try again", this one is the request
    never having arrived.
    """
    last: Exception | None = None
    for attempt in range(1, _TRANSPORT_RETRIES + 2):
        try:
            return await send()
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, httpx.WriteError) as exc:
            last = exc
            if attempt > _TRANSPORT_RETRIES:
                break
            logger.warning("%s: transport error (%s), retrying %d/%d", what, type(exc).__name__, attempt, _TRANSPORT_RETRIES)
            await asyncio.sleep(0.2 * attempt)
    assert last is not None
    raise last


# --- Write serialization + retry -------------------------------------------
# Real measured failure this fixes: eight CONCURRENT writes to the same case
# returned HTTP 503. Every write transactionally increments a single `seq`
# field on one case document (services/state_service/store.py), so concurrent
# writers contend on that one document and some lose all five transaction
# attempts. The docs' concurrency model has Perception, Error Detection's
# per-window fan-out, and six event-driven agents all writing one case at once,
# so this is not an edge case — it is the normal operating condition.
#
# Two layers:
#   1. A per-case asyncio.Lock, so a single process never contends with
#      ITSELF. Since one case is owned by one orchestrator task, this removes
#      essentially all of the contention.
#   2. Retry with backoff on 503, for whatever contention remains (another
#      process, another instance). Specified in docs/agentic_workflow.md §9.
#
# The remaining cost is throughput, not correctness: writes to one case
# serialize at roughly one per second. See the module docstring note.
_case_write_locks: dict[tuple[object, str], asyncio.Lock] = {}

_RETRY_BACKOFF_S = (0.5, 1.5, 4.0)


def _get_case_lock(case_id: str) -> asyncio.Lock:
    key = (asyncio.get_running_loop(), case_id)
    lock = _case_write_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _case_write_locks[key] = lock
    return lock


async def _post_patch_async(state_service_url: str, case_id: str, event: StateDiffEvent) -> StateDiffEvent:
    client = _get_async_client()
    url = f"{state_service_url}/state/{case_id}/patch"
    payload = event.model_dump(mode="json")

    async with _get_case_lock(case_id):
        last_error: Exception | None = None
        for attempt, backoff in enumerate((*_RETRY_BACKOFF_S, None)):
            resp = await _request_with_retry(lambda: client.post(url, json=payload), f"patch {case_id}")
            if resp.status_code != 503:
                resp.raise_for_status()
                # The server's response is authoritative (real seq assigned
                # there) — returning our own pre-request copy would silently
                # misreport it.
                return StateDiffEvent.model_validate(resp.json())

            # 503 means transaction contention specifically, which is
            # retryable. Any other error is not, and is raised above.
            last_error = httpx.HTTPStatusError(
                f"state service returned 503 for case {case_id} (transaction contention)",
                request=resp.request,
                response=resp,
            )
            if backoff is None:
                break
            logger.warning(
                "state write contended for %s (attempt %d), retrying in %.1fs", case_id, attempt + 1, backoff
            )
            await asyncio.sleep(backoff)

        assert last_error is not None
        raise last_error


async def _get_snapshot_async(state_service_url: str, case_id: str) -> StateSnapshot:
    client = _get_async_client()
    resp = await _request_with_retry(
        lambda: client.get(f"{state_service_url}/state/{case_id}/snapshot"), f"snapshot {case_id}"
    )
    resp.raise_for_status()
    return StateSnapshot.model_validate(resp.json())
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
        committed = await _post_patch_async(state_service_url, case_id, event)
    else:
        # The local-file fallback is genuinely blocking file I/O, so it still
        # goes to a thread — but it is only ever used when no state service is
        # configured, i.e. scripts and tests, never under real concurrency.
        committed = await asyncio.to_thread(_write_local_fallback_sync, case_id, event)

    # Publish AFTER the write commits, so an event-driven agent reading the
    # graph always sees the state that triggered it — publishing first would
    # race a handler's own snapshot fetch against the write it fired on.
    # Never awaited (state/event_bus.py::publish spawns tasks): a cheap write
    # must not block behind whatever multi-second reasoning it kicks off.
    # No-op in a process with no subscribers (scripts, tests).
    event_bus.publish(case_id, committed)
    return committed


async def apply_state_patches(case_id: str, patches: list[tuple[GraphNodePatch | None, GraphEdgePatch | None, str]]) -> list[StateDiffEvent]:
    """Batch form of apply_state_patch — one round trip, one transaction.

    Use this wherever a caller already has several writes in hand (a perception
    window's entity updates, an error-detection window's sub-agent edges).
    Single writes to one case serialize at roughly a second each, so a window
    that emits ten of them individually costs ten seconds against a five-second
    cadence; batched, it costs one round trip.

    Order is preserved end to end: the store assigns consecutive seq values in
    list order, so the SSE stream sees exactly the intended order.
    """
    if not patches:
        return []

    events = [
        StateDiffEvent(
            case_id=case_id,
            seq=0,  # the store is the sole authority; this is a placeholder
            op="add_node" if node is not None else "add_edge",
            node=node,
            edge=edge,
            reason=reason,
            source_agent=(node.source_agent if node else edge.source_agent),
            source_tool=(node.source_tool if node else edge.source_tool),
        )
        for node, edge, reason in patches
    ]

    state_service_url = os.environ.get("STATE_SERVICE_URL")
    if state_service_url:
        client = _get_async_client()
        async with _get_case_lock(case_id):
            resp = await _request_with_retry(
                lambda: client.post(
                    f"{state_service_url}/state/{case_id}/patch/batch",
                    json=[e.model_dump(mode="json") for e in events],
                ),
                f"batch patch {case_id}",
            )
            resp.raise_for_status()
            committed = [StateDiffEvent.model_validate(item) for item in resp.json()]
    else:
        committed = [await asyncio.to_thread(_write_local_fallback_sync, case_id, e) for e in events]

    for event in committed:
        event_bus.publish(case_id, event)
    return committed


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
        return await _get_snapshot_async(state_service_url, case_id)
    return await asyncio.to_thread(_read_local_fallback_snapshot_sync, case_id)
