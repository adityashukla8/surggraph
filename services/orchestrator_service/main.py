"""Orchestrator service — the entry point the frontend calls to actually
start the autonomous pipeline.

The trigger (plan §11, revised after discussion): the user pressing play on
the video for the first time — not page load, not a GCS/Eventarc pipeline.
The frontend calls POST /cases/open exactly once, which mints a brand-new,
fully isolated case_id and kicks off Orchestrator's real work as a
background task. Every concurrent user gets their own independent pipeline
run — no get-or-create, no shared state — see agents/orchestrator/agent.py
and services/state_service/store.py's module docstrings for the full
multi-tenant design and why (real per-user isolation was an explicit,
non-negotiable requirement, not a nice-to-have).
"""

from __future__ import annotations

import logging
import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.cloud import firestore
from typing import Literal

from pydantic import BaseModel

from agents.hitl.approval import DraftNotFound, record_approval
from agents.hitl.acknowledgment import (
    NotAProposal,
    ProposalNotFound,
    record_acknowledgment,
)
from agents.orchestrator.agent import open_case
from tools.observability import setup_cloud_observability

load_dotenv()
setup_cloud_observability("surggraph-orchestrator-service")

# Same real bug found and fixed earlier this session in services/surgbot_
# service/main.py: nothing in this app ever called logging.basicConfig(),
# so Python's root logger had no handler attached at all — every
# logger.info() call across the whole pipeline (every agent under agents/,
# tools/adk_runner.py, tools/medgemma_model.py, ...) was being silently
# dropped. Root level set to INFO so real logs actually appear; known-noisy
# third-party loggers turned back down explicitly rather than silencing
# INFO globally.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy_logger in ("httpx", "httpcore", "google", "google.auth", "urllib3", "websockets", "asyncio"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

app = FastAPI(title="SurgGraph Orchestrator Service")

_cors_origins_env = os.environ.get("ORCHESTRATOR_SERVICE_CORS_ORIGINS")
_cors_origins = _cors_origins_env.split(",") if _cors_origins_env else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Same local-dev pattern as services/state_service/main.py — localhost
    # and 127.0.0.1 are different origins to the browser even though they
    # resolve to the same host.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_firestore_client: firestore.AsyncClient | None = None


def _get_firestore_client() -> firestore.AsyncClient:
    global _firestore_client
    if _firestore_client is None:
        _firestore_client = firestore.AsyncClient(database=os.environ.get("FIRESTORE_DATABASE", "(default)"))
    return _firestore_client


class OpenCaseRequest(BaseModel):
    video_id: str


class OpenCaseResponse(BaseModel):
    case_id: str


@app.post("/cases/open", response_model=OpenCaseResponse)
async def post_open_case(payload: OpenCaseRequest, background_tasks: BackgroundTasks) -> OpenCaseResponse:
    """Mints a fresh case_id, records it in Firestore (case_id/video_id/
    created_at — the case doc's `seq` field is initialized by the first
    real graph write inside open_case, not here), and schedules the actual
    pipeline run via FastAPI's BackgroundTasks — not a bare
    asyncio.create_task, which risks premature garbage collection if the
    reference isn't held. Returns immediately; the frontend follows up by
    connecting to the state service's SSE stream for this case_id."""
    case_id = f"case-{uuid.uuid4().hex[:12]}"

    client = _get_firestore_client()
    await client.collection("cases").document(case_id).set(
        {
            "case_id": case_id,
            "video_id": payload.video_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        merge=True,
    )

    background_tasks.add_task(open_case, case_id, payload.video_id)

    return OpenCaseResponse(case_id=case_id)


class HitlAcknowledgmentRequest(BaseModel):
    proposal_node_id: str
    outcome: Literal["acknowledged", "dismissed"]


class HitlApprovalRequest(BaseModel):
    outcome: Literal["approved", "rejected", "edited"]
    edited_sections: dict | None = None


# --- MedGemma warm-up -------------------------------------------------------
# MedGemma scales to zero, and waking it takes minutes. It is only needed at
# the very END of a case (documentation ran 181s after case start in the last
# real run), so a throwaway request fired the moment a visitor lands gives the
# GPU a head start it would otherwise have to find during the case itself.
#
# It has to be proxied: the Vertex endpoint is IAM-authed, so a browser cannot
# reach it directly. Fire-and-forget — the caller gets 202 immediately and
# never waits on the GPU.
_last_warm = 0.0
_WARM_EVERY_S = 120.0


async def _warm_medgemma() -> None:
    from tools import medgemma_model

    if not medgemma_model.medgemma_available():
        return
    try:
        # Smallest possible real generation: enough to trigger scale-up,
        # cheap enough to be free once warm.
        await asyncio.to_thread(medgemma_model._predict_sync, "OK", 1)
        logger.info("warm[medgemma]: already warm")
    except Exception as exc:  # noqa: BLE001 — a 429 here is the POINT: it means scale-up started
        logger.info("warm[medgemma]: wake-up triggered (%s)", str(exc)[:120])


@app.post("/warm", status_code=202)
async def post_warm(background_tasks: BackgroundTasks) -> dict:
    """Nudges MedGemma awake. Safe to call on every page view."""
    global _last_warm
    now = time.monotonic()
    if now - _last_warm < _WARM_EVERY_S:
        return {"warming": False, "reason": "recently warmed"}
    _last_warm = now
    background_tasks.add_task(_warm_medgemma)
    return {"warming": True}


@app.post("/cases/{case_id}/hitl/approval")
async def post_hitl_approval(case_id: str, payload: HitlApprovalRequest) -> dict:
    """HITL #2 — the surgeon approving, editing or rejecting the operative record.

    Synchronous and end-to-end: approve, gate, file, report. There is no
    awaiting_approval state to park in, because a coroutine held open for a
    decision that may take hours does not survive a restart — the draft's own
    approval_status on the graph is the durable state instead.
    """
    try:
        return await record_approval(case_id, payload.outcome, payload.edited_sections)
    except DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.post("/cases/{case_id}/hitl/acknowledgment")
async def post_hitl_acknowledgment(case_id: str, payload: HitlAcknowledgmentRequest) -> dict:
    """HITL #1 — the surgeon acknowledging or dismissing a corrective proposal.

    On the orchestrator rather than the state service, which docs §11 names as
    the sole HITL channel. Interpreting an acknowledgment is surgical domain
    knowledge, and plan_v2 §4.3 requires the state service to know nothing
    about surgery — see agents/hitl/acknowledgment.py for the full reasoning.

    Synchronous, unlike case opening: a surgeon tapping acknowledge needs to
    know it landed, and a background task returning 200 immediately would
    report success before anything had changed.
    """
    try:
        return await record_acknowledgment(case_id, payload.proposal_node_id, payload.outcome)
    except ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except NotAProposal as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
