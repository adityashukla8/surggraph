"""The Living State Graph service — single writer of CaseState's graph
(initial_11082026.md §9: "state is queried via tools, not carried in
prompt context"). Three endpoints, matching exactly what
ui/frontend/src/graph/useCaseStateStream.ts and App.tsx already expect:

  GET  /state/{case_id}/snapshot  — full current graph
  GET  /state/{case_id}/stream    — SSE, state_diff events + heartbeats
  POST /events/manual             — tile 4's manual injection box

Deliberately domain-agnostic: this service knows nothing about surgery,
Monitor Agent, or CARES categories — it's a generic graph store + pub/sub
relay. Anything DivergenceEvent-specific (matching a manual injection to a
pending TrajectoryPatch, real phase/frame anchoring) belongs in an agent
that has that context (Orchestrator, once it exists), not here.
"""

from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from services.state_service import gcs_video
from services.state_service.store import TransactionContentionError, store
from state import node_ids
from state.schema import GraphNodePatch, StateDiffEvent, StateSnapshot

load_dotenv()

app = FastAPI(title="SurgGraph State Service")


@app.get("/media/video/{video_id}/{filename}")
async def get_video(video_id: str, filename: str, request: Request) -> Response:
    """Streams the prepared demo video (scripts/prepare_demo_videos.py's
    output) straight from Cloud Storage — no local-disk dependency, so this
    works identically on a laptop or a stateless, fresh Cloud Run instance
    that has never touched data/video/ (see gcs_video.py's module docstring
    for why this matters and how Range requests are forwarded)."""
    return await gcs_video.stream_video(video_id, filename, request)

_cors_origins_env = os.environ.get("STATE_SERVICE_CORS_ORIGINS")
_cors_origins = _cors_origins_env.split(",") if _cors_origins_env else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Local dev's frontend is reachable via localhost or 127.0.0.1 — the
    # browser treats these as different origins even though they resolve to
    # the same host (confirmed: the earlier config only allowed the exact
    # string "http://localhost:5173" and broke when accessed via
    # 127.0.0.1:5173 instead). Matching the general local-dev pattern here,
    # not a second guessed exact string. STATE_SERVICE_CORS_ORIGINS (comma-
    # separated, explicit) is additive, for non-local deployments where a
    # regex would be too permissive.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/state/{case_id}/snapshot", response_model=StateSnapshot)
async def get_snapshot(case_id: str) -> StateSnapshot:
    return await store.snapshot(case_id)


@app.post("/state/{case_id}/patch", response_model=StateDiffEvent)
async def post_patch(case_id: str, incoming: StateDiffEvent) -> StateDiffEvent:
    if incoming.node is None and incoming.edge is None:
        raise HTTPException(status_code=400, detail="patch requires a node or an edge")
    try:
        return await store.apply_patch(case_id, incoming)
    except TransactionContentionError:
        # Real but rare (see store.py) — two writers hit the same case doc
        # in the same instant and the Firestore transaction's 5 retries were
        # exhausted. Retryable, not a caller error: surface 503, not 500, so
        # a client (or a future retry wrapper around apply_state_patch)
        # knows to try again rather than treating this as fatal.
        raise HTTPException(status_code=503, detail="transaction contention, retry") from None


@app.post("/state/{case_id}/patch/batch", response_model=list[StateDiffEvent])
async def post_patch_batch(case_id: str, incoming: list[StateDiffEvent]) -> list[StateDiffEvent]:
    """N graph items in one transaction. Same contention semantics as the
    single-item endpoint, but one contention point per batch instead of N."""
    for event in incoming:
        if event.node is None and event.edge is None:
            raise HTTPException(status_code=400, detail="every patch requires a node or an edge")
    try:
        return await store.apply_patch_batch(case_id, incoming)
    except TransactionContentionError:
        raise HTTPException(status_code=503, detail="transaction contention, retry") from None


@app.get("/state/{case_id}/stream")
async def stream(case_id: str, request: Request, since_seq: int | None = None) -> EventSourceResponse:
    """`since_seq` is the resume point — pass the seq of the snapshot you just
    fetched. Omitting it re-opens the snapshot/stream gap documented on
    store.subscribe(); it stays optional only so an ad-hoc curl still works."""
    queue, watch = await store.subscribe(case_id, since_seq=since_seq)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                event = await queue.get()
                yield {"event": "state_diff", "data": event.model_dump_json()}
        finally:
            await store.unsubscribe(case_id, watch)

    # ping=15: periodic SSE comment every 15s so intermediate proxies (e.g.
    # Cloud Run) don't time out an idle connection — the plan's own flagged
    # open risk ("SSE over Cloud Run assumes no intermediate proxy strips
    # long-lived connections — don't assume") gets a real answer once this
    # is actually deployed and smoke-tested there, not just assumed here.
    return EventSourceResponse(event_generator(), ping=15)


class ManualEventRequest(BaseModel):
    case_id: str
    text: str


@app.post("/events/manual", response_model=StateDiffEvent)
async def post_manual_event(payload: ManualEventRequest) -> StateDiffEvent:
    """Relays a human-typed event onto the graph as a plain, honestly-tagged
    node — source_agent="human", not disguised as an agent's inference, no
    fabricated category/confidence/window data. Turning this into a real
    DivergenceEvent (matched against pending TrajectoryPatches, anchored to
    a real current phase/frame) needs live case-position context this
    service doesn't have — that's Orchestrator's job once it exists, not
    something to fake here."""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    node = GraphNodePatch(
        node_id=node_ids.manual_event(uuid.uuid4().hex[:8]),
        node_type="manual_event",
        label=f"Manual: {payload.text[:60]}",
        attrs={"text": payload.text},
        source_agent="human",
        source_tool="manual_event_injection",
    )
    patch = StateDiffEvent(
        case_id=payload.case_id,
        seq=0,  # ignored — store assigns the real value
        op="add_node",
        node=node,
        reason=payload.text,
        source_agent="human",
        source_tool="manual_event_injection",
    )
    return await store.apply_patch(payload.case_id, patch)
