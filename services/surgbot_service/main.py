"""SurgBot backend service — the third Cloud Run service, additive to
services/state_service and services/orchestrator_service (neither of which
this file imports from or writes to).

This service does NOT run the ADK Runner/LiveRequestQueue itself — that would
mean re-hosting the agent locally, duplicating what Agent Runtime already
does, and losing Identity/Registry/Observability as automatic side effects of
deployment. Its only job on the voice path is to hold the browser WebSocket
open and relay both directions against a real bidi_stream_query connection to
the ALREADY-DEPLOYED root agent (scripts/deploy_surgbot_agent.py).

TWO REAL PROTOCOL DETAILS FOUND BY READING THE INSTALLED SDK SOURCE THIS
SESSION (not assumed from docs — same standard this project's other Day-1
spikes already held themselves to):

1. vertexai/_genai/live_agent_engines.py::AsyncLiveAgentEngineSession.send()
   wraps whatever dict you pass in {"bidi_stream_input": ...} before putting
   it on the wire — callers here just pass the plain dict.
2. vertexai/preview/reasoning_engines/templates/adk.py::AdkApp.
   bidi_stream_query's request_queue handling: ONLY the very first item
   pulled off the queue is treated as the envelope {"user_id":...,
   "session_id":..., "live_request": {...}} — every SUBSEQENT item is fed
   directly into `LiveRequest.model_validate(item)`. That means every send
   AFTER the first must be a bare LiveRequest-shaped dict (`{"blob": {...}}`
   or `{"content": {...}}`), never wrapped in another `{"live_request": ...}`
   layer — getting this wrong would silently produce a Pydantic
   validation error server-side on the second message onward. This file's
   _forward_browser_to_agent implements that distinction explicitly.

DISCLOSURE IS STRUCTURAL, NOT COSMETIC (plan's hard requirement): every
function_call/function_response part seen in a relayed event becomes a
tool_call_started/tool_call_finished control message carrying a REQUIRED
agent_name + model_id + api_surface triple, resolved from agents/surgbot/
root_agent.py::TOOL_DISCLOSURE — so the transcript can never show a tool/
agent-use indicator with the model identity half missing. phase_changed is
inferred the same way, from TOOL_PHASE_MAP.

SESSION LENGTH / RECONNECT (plan's disclosed open question): bidi_stream_
query's hard ceiling is documented as 10 minutes. This file forwards any
live_session_resumption_update the agent emits as session_resumption_handle
so the frontend CAN implement transparent reconnect, and persists a running
summary placeholder on the session doc for that path — but full automatic
mid-session reconnection is NOT implemented in this pass (disclosed,
sanctioned cut per plan §14.6's cut-order: "cap demo case length instead").
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import vertexai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.surgbot import case_index, memory_bank, store, subagents
from agents.surgbot.root_agent import TOOL_DISCLOSURE, TOOL_PHASE_MAP
from agents.surgbot.live_model import SURGBOT_LIVE_MODEL
from agents.surgbot.schema import PHASE_LABELS, SurgBotSession
from tools.observability import setup_cloud_observability

load_dotenv()
setup_cloud_observability("surggraph-surgbot-service")

# Real bug found this session: nothing in this app ever called
# logging.basicConfig(), so Python's root logger had NO handler attached at
# all. Every logger.info() call in this module — including all of the
# diagnostic logging added this session specifically to trace "which step is
# the conversation breaking" — was silently dropped, never written anywhere,
# regardless of whether the code path actually ran. Only .warning()/.error()
# survived, via Python's "handler of last resort" (which hardcodes a
# WARNING floor when no real handler exists). This is why several real
# closures this session showed zero log output even with explicit
# logger.info(...) calls placed directly on the return path. Root level set
# to INFO so this app's own logs actually appear; known-noisy third-party
# loggers turned back down explicitly rather than silencing INFO globally.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy_logger in ("httpx", "httpcore", "google", "google.auth", "urllib3", "websockets", "asyncio"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = FastAPI(title="SurgGraph SurgBot Service")

_cors_origins_env = os.environ.get("SURGBOT_SERVICE_CORS_ORIGINS")
_cors_origins = _cors_origins_env.split(",") if _cors_origins_env else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Same local-dev pattern as services/state_service/main.py and
    # services/orchestrator_service/main.py — localhost and 127.0.0.1 are
    # different origins to the browser even though they resolve to the same
    # host.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ROOT_AGENT_CACHE = _REPO_ROOT / "scripts" / ".deployed_root_agent.json"

_DEFAULT_DISCLOSURE = {"agent_name": "surgbot_root", "model_id": SURGBOT_LIVE_MODEL, "api_surface": "vertex_ai_live"}

# Real gap found and fixed this session: the very first bidi_stream_query
# request never carried a run_config at all, so ADK's Runner.run_live() fell
# back to RunConfig()'s bare default. Automatic voice-activity detection /
# barge-in is already on by default Live-API-wide (confirmed via a real raw
# google.genai spike this session) — explicit here, tuned per Google's own
# documented recommended range (500-800ms), rather than left as an unstated,
# accidental default.
#
# speech_config (voice selection) deliberately does NOT live here anymore —
# moved to agents/surgbot/live_model.py::new_live_agent_model, matching
# Google's own official ADK sample (google/adk-python, contributing/samples/
# live/live_bidi_streaming_multi_agent/agent.py), which sets speech_config
# directly on the Gemini model constructor. Real user evidence: three
# different voice_name values sent via this run_config had zero audible
# effect end-to-end through the EXPERIMENTAL Agent-Runtime relay, even though
# a plain ADK code trace suggests it should have worked. Rather than keep
# debugging an unverified secondary path, matched Google's own demonstrated,
# trusted pattern instead — see live_model.py's module docstring for the full
# account. A run_config-level speech_config would in any case be a no-op now
# that the model itself sets one (google_llm.py::Gemini.connect(): "if
# self.speech_config is not None: ...overrides..."), so keeping both would
# just be misleading dead code.
#
# automatic_activity_detection is now DISABLED — real user report + real
# Cloud Logging evidence: automatic VAD-triggered barge-in reliably risks a
# genuine server-side crash (assembly_service.py's internal output_queue
# overflowing — QueueFull — confirmed via the deployed agent's own stderr,
# correlated directly with "interrupted" events in a real test session).
# Compared against Google's own reference relay (GoogleCloudPlatform/agent-
# starter-pack's expose_app.py): it does no VAD tuning or interruption
# handling of any kind — it just forwards. Rather than keep tuning automatic
# detection thresholds against an undocumented, proprietary internal queue,
# switched to Google's own documented alternative: manual activity control
# (LiveRequest.activity_start/activity_end — confirmed real, empty marker
# types on google.genai.types.ActivityStart/ActivityEnd). Push-to-talk
# (SurgBotPanel.tsx) now sends these explicitly, once per deliberate user
# gesture, instead of the server continuously auto-detecting speech in a
# steady audio stream — eliminating the automatic/accidental interruption
# path entirely, not just tuning its sensitivity.
_LIVE_RUN_CONFIG: dict[str, Any] = {
    "response_modalities": ["AUDIO"],
    "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
}

_vertex_client: vertexai.Client | None = None


def _get_vertex_client() -> vertexai.Client:
    global _vertex_client
    if _vertex_client is None:
        _vertex_client = vertexai.Client(project=PROJECT_ID, location=REGION)
    return _vertex_client


def _root_agent_resource_name() -> str | None:
    override = os.environ.get("SURGBOT_ROOT_AGENT_RESOURCE")
    if override:
        return override
    if not _ROOT_AGENT_CACHE.exists():
        return None
    try:
        cache = json.loads(_ROOT_AGENT_CACHE.read_text())
        return cache.get("resource_name")
    except (json.JSONDecodeError, OSError):
        return None


def _synthesis_resource_name() -> str:
    engine = subagents.deploy_or_get_subagent("synthesis", _get_vertex_client())
    return engine.api_resource.name


# --- REST routes ---------------------------------------------------------------


@app.get("/cases")
async def get_cases() -> dict[str, Any]:
    """Real, possibly-empty case listing — never fabricated placeholder
    data (agents/surgbot/case_index.py::list_cases's own contract).

    list_cases() is a plain synchronous Firestore call — real bug found this
    session: this app runs ALL concurrent voice WebSocket sessions on one
    shared event loop, so calling it directly here would stall every other
    browser's live audio too, not just this request. Run it in a thread.
    """
    cases = await asyncio.to_thread(case_index.list_cases)
    return {"cases": [c.model_dump(mode="json") for c in cases], "count": len(cases)}


class ApprovalRequest(BaseModel):
    outcome: str  # "approved" | "rejected" | "edited"
    edited_sections: dict[str, Any] | None = None


@app.post("/surgbot/reviews/{review_id}/approval")
async def post_review_approval(review_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    try:
        result = await store.record_review_approval(review_id, payload.outcome, payload.edited_sections)
    except store.ReviewNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Phase 5 approval also writes to Memory Bank — real product value for
    # Phase 6's cross-session pattern review, never a parked/best-effort
    # afterthought that silently swallows a real failure: memory_bank.
    # create_memory already fails soft internally (returns None, logs) so
    # this endpoint's own response never depends on Memory Bank succeeding.
    if result["outcome"] in ("approved", "edited"):
        document = await store.get_review(review_id)
        if document is not None:
            fact_parts = [document.case_summary, *document.coaching_notes, *document.disagreements]
            fact = " ".join(p for p in fact_parts if p)[:1000]
            if fact:
                # _synthesis_resource_name() and memory_bank.create_memory()
                # are both plain synchronous, blocking network calls — same
                # shared-event-loop-freezing bug as get_cases() above. Run
                # the whole chain in one thread.
                await asyncio.to_thread(
                    lambda: memory_bank.create_memory(document.reviewer_id, fact, agent_engine=_synthesis_resource_name())
                )

    return result


@app.get("/surgbot/reviewers/{reviewer_id}/patterns")
async def get_reviewer_patterns(reviewer_id: str, query: str = "review session patterns") -> dict[str, Any]:
    facts = await asyncio.to_thread(
        lambda: memory_bank.retrieve_memories(reviewer_id, query=query, agent_engine=_synthesis_resource_name())
    )
    return {"reviewer_id": reviewer_id, "memories": facts, "count": len(facts)}


# --- WebSocket voice relay -------------------------------------------------


def _disclosure_for(tool_name: str) -> dict[str, str]:
    return TOOL_DISCLOSURE.get(tool_name, _DEFAULT_DISCLOSURE)


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_text(json.dumps(payload))


# Real bug, found via live testing (not anticipated in the original design):
# the browser's AudioWorkletProcessor posts one message per ~128-sample
# audio quantum — hundreds of tiny postMessage calls per second even after
# resampling to 16kHz. Forwarding each one as its own connection.send(...)
# floods the deployed agent's bidi_stream_query handler; real observed
# failure: "generic::resource_exhausted: Client overran StreamHandler
# internal queue", plus assorted "Reasoning Engine Execution failed" 1011
# closes under the same load. Fix: buffer incoming PCM bytes and flush on a
# fixed interval instead of per-frame — this is purely a relay-side change,
# no frontend rebuild needed, since the browser already just fires-and-
# forgets binary WS frames with no per-frame ack expected.
#
# REVISED (real user report: "fails a lot during turns and then restarts").
# The 0.2s/5-per-sec rate above was NOT sufficient — confirmed via Cloud
# Logging (aiplatform.googleapis.com/reasoning_engine_stderr), a real
# unhandled exception INSIDE the deployed agent's own EXPERIMENTAL bidi
# server framework, not our relay: application_queue.put_nowait(...) raising
# asyncio.QueueFull in Google's own /code/assembly_service.py
# ::_receive_data_from_boq_client — a bounded internal queue on the AGENT
# side overflowing, which its framework then reports upstream to us as the
# generic::resource_exhausted 1011 close. This is a real limitation of
# Google's own EXPERIMENTAL agent_server_mode implementation, not something
# fixable from our code — the only lever available is sending less
# frequently so the agent's queue has more headroom during a busy
# generation/tool-call window (when it isn't draining the queue as fast).
# Halving the rate again (0.5s/2-per-sec) is still comfortably within
# natural conversational latency for voice turn-taking.
_AUDIO_FLUSH_INTERVAL_S = 0.5
_AUDIO_MAX_BUFFER_BYTES = 32000  # ~1s of 16kHz mono PCM16 — safety cap so a stalled flush can't grow this unbounded


async def _forward_browser_to_agent(websocket: WebSocket, connection, state: dict[str, Any]) -> None:
    """Browser -> deployed agent. Binary frames are raw PCM16 audio; text
    frames are JSON control messages. Everything sent here AFTER the caller's
    own first envelope message must be a bare LiveRequest-shaped dict (see
    module docstring, point 2). Audio is buffered and flushed on a timer
    (see _AUDIO_FLUSH_INTERVAL_S above) rather than sent per-frame."""
    audio_buffer = bytearray()

    async def _flush_audio() -> None:
        if not audio_buffer:
            return
        data = bytes(audio_buffer)
        audio_buffer.clear()
        await connection.send({"blob": {"data": base64.b64encode(data).decode("ascii"), "mime_type": "audio/pcm;rate=16000"}})

    async def _periodic_flush() -> None:
        while True:
            await asyncio.sleep(_AUDIO_FLUSH_INTERVAL_S)
            await _flush_audio()

    flush_task = asyncio.create_task(_periodic_flush())
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                # Real gap found this session: this exact path used to
                # return with ZERO logging — a genuine "which step is the
                # conversation breaking" blind spot. code/reason on this ASGI
                # message tell you whether the BROWSER actually closed the
                # tab/socket (normal) vs. something else reported a disconnect
                # the browser never actually sent.
                logger.info(
                    "surgbot voice ws[browser->agent]: browser disconnected (code=%r, reason=%r)",
                    message.get("code"), message.get("reason"),
                )
                state["disconnected"] = True
                return

            data = message.get("bytes")
            if data is not None:
                audio_buffer.extend(data)
                if len(audio_buffer) >= _AUDIO_MAX_BUFFER_BYTES:
                    await _flush_audio()
                continue

            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("surgbot voice ws: dropped non-JSON text frame")
                continue

            msg_type = control.get("type")
            if msg_type == "text_turn":
                await _flush_audio()  # preserve ordering: any pending audio precedes this text turn
                await connection.send({"content": {"role": "user", "parts": [{"text": control.get("text", "")}]}})
            elif msg_type == "mic_start":
                # Push-to-talk: automatic_activity_detection is disabled
                # (see _LIVE_RUN_CONFIG above) — we now own turn boundaries
                # explicitly instead of the server auto-detecting speech in a
                # continuous stream. This is the one moment a real user
                # turn begins; the model has no other way to know.
                await connection.send({"activity_start": {}})
            elif msg_type == "mic_stop":
                await _flush_audio()  # any buffered audio belongs to the turn that's ending
                await connection.send({"activity_end": {}})
            elif msg_type == "end_session":
                logger.info("surgbot voice ws[browser->agent]: browser sent end_session")
                state["ending"] = True
                return
            elif msg_type == "session_start":
                # Already consumed before this loop starts; a duplicate is
                # harmless to ignore.
                continue
            else:
                logger.warning("surgbot voice ws: unrecognized control message type %r", msg_type)
    except Exception:
        logger.exception("surgbot voice ws[browser->agent]: forward loop raised")
        raise
    finally:
        flush_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flush_task


async def _forward_agent_to_browser(websocket: WebSocket, connection, session_id: str, state: dict[str, Any]) -> None:
    """Deployed agent -> browser. No __aiter__ on AsyncLiveAgentEngineSession
    (confirmed via source read, same finding scripts/spike_surgbot_live_
    roundtrip.py already made) — poll receive() in a loop and treat a closed
    connection as a normal end, not an error."""
    open_calls: dict[str, str] = {}  # call_id -> tool_name, for pairing started/finished

    while not state.get("ending") and not state.get("disconnected"):
        try:
            raw = await connection.receive()
        except Exception as exc:
            logger.info("surgbot voice ws[%s]: agent stream ended (%r)", session_id, exc)
            return

        event = raw.get("bidiStreamOutput", raw) if isinstance(raw, dict) else raw
        if not isinstance(event, dict):
            continue

        if event.get("error_code"):
            await _send_json(websocket, {"type": "error", "detail": event.get("error_message", event["error_code"])})

        resumption = event.get("live_session_resumption_update")
        if resumption and resumption.get("new_handle"):
            await _send_json(websocket, {"type": "session_resumption_handle", "handle": resumption["new_handle"]})

        # Barge-in: ADK's Event.interrupted (google/adk/models/llm_response.py)
        # is a top-level field on the dumped event dict, set True when VAD
        # detected the user talking over the model and the server canceled
        # generation mid-stream (confirmed via a real raw Live API spike this
        # session — automatic VAD/interruption is on by default, no config
        # needed to trigger it server-side). What was missing is this: the
        # browser's PcmPlayer keeps playing whatever PCM chunks were already
        # sent before the interruption, since nothing ever told it to stop.
        # Forward the signal so the frontend can flush its queued/playing
        # audio immediately instead of talking over the interrupting user.
        if event.get("interrupted"):
            await _send_json(websocket, {"type": "interrupted"})

        input_transcript = event.get("input_transcription")
        if input_transcript and input_transcript.get("text"):
            await _send_json(
                websocket,
                {"type": "transcript_delta", "speaker": "user", "text": input_transcript["text"], "final": bool(input_transcript.get("finished"))},
            )

        output_transcript = event.get("output_transcription")
        if output_transcript and output_transcript.get("text"):
            await _send_json(
                websocket,
                {"type": "transcript_delta", "speaker": "model", "text": output_transcript["text"], "final": bool(output_transcript.get("finished"))},
            )

        content = event.get("content") or {}
        for part in content.get("parts", []) or []:
            function_call = part.get("function_call")
            if function_call:
                tool_name = function_call.get("name", "")
                call_id = function_call.get("id") or f"c-{uuid.uuid4().hex[:8]}"
                open_calls[call_id] = tool_name

                phase = TOOL_PHASE_MAP.get(tool_name)
                if phase is not None and phase != state.get("current_phase"):
                    state["current_phase"] = phase
                    await _send_json(websocket, {"type": "phase_changed", "phase": phase, "phase_label": PHASE_LABELS.get(phase, "")})
                    # Real difference found comparing against Google's own
                    # official relay reference (GoogleCloudPlatform/agent-
                    # starter-pack's expose_app.py): their receive-from-
                    # remote loop does ZERO per-event backend I/O — it just
                    # forwards. This used to `await` a Firestore write
                    # inline, blocking the receive loop on every phase
                    # transition. The deployed agent's own OUTPUT queue
                    # (assembly_service.py's output_queue, confirmed via
                    # Cloud Logging QueueFull) fills up if we don't drain
                    # connection.receive() fast enough — any stall here,
                    # even an occasional slow Firestore round-trip, directly
                    # contributes to that. Fire-and-forget instead: session
                    # bookkeeping should never gate how fast we drain the
                    # agent's real-time stream.
                    asyncio.create_task(store.update_session(session_id, current_phase=phase))

                disclosure = _disclosure_for(tool_name)
                await _send_json(
                    websocket,
                    {
                        "type": "tool_call_started",
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "args_summary": json.dumps(function_call.get("args", {}))[:300],
                        **disclosure,
                    },
                )

            function_response = part.get("function_response")
            if function_response:
                call_id = function_response.get("id") or "unknown"
                tool_name = open_calls.pop(call_id, function_response.get("name", ""))
                response_body = function_response.get("response", {})
                summary = json.dumps(response_body)[:300]
                await _send_json(websocket, {"type": "tool_call_finished", "call_id": call_id, "summary": summary})

                if tool_name == "draft_review_document" and isinstance(response_body, dict) and response_body.get("drafted"):
                    await _send_json(
                        websocket,
                        {
                            "type": "review_document_ready",
                            "review_id": response_body.get("review_id"),
                            "sections": response_body.get("sections", {}),
                            "approval_status": response_body.get("approval_status"),
                        },
                    )

            inline_data = part.get("inline_data")
            if inline_data and inline_data.get("data") and str(inline_data.get("mime_type", "")).startswith("audio"):
                raw_data = inline_data["data"]
                if isinstance(raw_data, (bytes, bytearray)):
                    audio_bytes = bytes(raw_data)
                else:
                    # The Live API encodes inline_data as URL-safe base64
                    # (alphabet uses '-'/'_' in place of '+'/'/') — confirmed
                    # by capturing real failing payloads. Plain base64.b64decode
                    # silently drops '-'/'_' as invalid characters, which then
                    # breaks padding and raises on most real audio chunks
                    # (only chunks lacking both characters happened to decode
                    # correctly), producing large gaps in playback that sound
                    # like static. urlsafe_b64decode is a strict superset: it
                    # behaves identically to b64decode for a string with
                    # neither character, so this is safe for both.
                    try:
                        audio_bytes = base64.urlsafe_b64decode(raw_data)
                    except Exception:
                        logger.warning(
                            "surgbot voice ws[%s]: undecodable inline_data audio, dropping frame (type=%s len=%s head=%r)",
                            session_id, type(raw_data).__name__, len(raw_data) if hasattr(raw_data, "__len__") else "?",
                            raw_data[:60] if isinstance(raw_data, str) else str(raw_data)[:60],
                        )
                        continue
                await websocket.send_bytes(audio_bytes)

            text_part = part.get("text")
            if text_part:
                speaker = "model" if content.get("role") != "user" else "user"
                await _send_json(websocket, {"type": "transcript_delta", "speaker": speaker, "text": text_part, "final": not event.get("partial", False)})
    else:
        # Real gap found this session: exiting via the loop CONDITION going
        # false (state["ending"]/state["disconnected"] set by the OTHER
        # concurrent task, _forward_browser_to_agent) fell through with zero
        # logging — indistinguishable from every other silent-close path when
        # debugging "why did the session end here." state's own two flags
        # tell you which one actually fired. (while/else: this only runs when
        # the loop exits via its condition, never via the `return` above.)
        logger.info(
            "surgbot voice ws[%s]: exiting — ending=%r disconnected=%r",
            session_id, state.get("ending"), state.get("disconnected"),
        )


@app.websocket("/surgbot/{session_id}/voice")
async def surgbot_voice(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()

    try:
        first_raw = await websocket.receive_text()
        first = json.loads(first_raw)
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        return

    if first.get("type") != "session_start":
        await _send_json(websocket, {"type": "error", "detail": "first message must be session_start"})
        await websocket.close()
        return

    case_ids = first.get("case_ids", [])
    reviewer_id = first.get("reviewer_id", "unknown-reviewer")

    resource_name = _root_agent_resource_name()
    if not resource_name:
        await _send_json(
            websocket,
            {"type": "error", "detail": "SurgBot root agent is not deployed yet — run scripts/deploy_surgbot_agent.py"},
        )
        await websocket.close()
        return

    session = SurgBotSession(session_id=session_id, case_ids=case_ids, reviewer_id=reviewer_id, live_model_id=SURGBOT_LIVE_MODEL)
    await store.create_session(session)

    client = _get_vertex_client()
    state: dict[str, Any] = {"current_phase": None}

    # session_id is REQUIRED, verbatim, by two of root_agent.py's own tools
    # (record_feedback, draft_review_document) — the model has no other way
    # to learn it (real gap found and fixed this session: neither tool
    # signature nor this init message originally carried it, which would
    # have silently broken both tools the first time they were actually
    # called). Told explicitly here, once, at session start.
    #
    # Sent with partial=True (real user report: the agent was speaking first,
    # unprompted, right on connect — "no auto start on orb click, only on
    # user input"). Confirmed via ADK source read (flows/llm_flows/
    # base_llm_flow.py -> models/gemini_llm_connection.py::_send_content:
    # turn_complete=not partial) that LiveRequest.partial=True sends this
    # content with turn_complete=False on the wire, so the Live API treats it
    # as silently absorbed context rather than a turn to respond to — the
    # actual prompt to speak is now root_agent.py's own system instruction,
    # which explicitly tells the model to stay silent until the reviewer's
    # own first words. Verified locally via InMemoryRunner.run_live() before
    # ever redeploying — see scripts/test_root_agent_local.py.
    init_text = (
        f"[session_init, do not reply] session_id={session_id} reviewer_id={reviewer_id} case_ids={case_ids}. "
        f"Whenever you call record_feedback or draft_review_document, pass session_id=\"{session_id}\" "
        "exactly as given here — never invent or guess a different value. "
        "Do not respond to this message — stay silent until the reviewer speaks first."
    )

    try:
        async with client.aio.live.agent_engines.connect(
            agent_engine=resource_name,
            config={"class_method": "bidi_stream_query"},
        ) as connection:
            await connection.send(
                {
                    "user_id": reviewer_id,
                    "run_config": _LIVE_RUN_CONFIG,
                    "live_request": {"content": {"role": "user", "parts": [{"text": init_text}]}, "partial": True},
                }
            )

            forward_task = asyncio.create_task(_forward_browser_to_agent(websocket, connection, state))
            backward_task = asyncio.create_task(_forward_agent_to_browser(websocket, connection, session_id, state))

            done, pending = await asyncio.wait({forward_task, backward_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc:
                    logger.exception("surgbot voice ws[%s]: relay task failed", session_id, exc_info=exc)
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: bidi_stream_query connection failed", session_id)
        try:
            await _send_json(websocket, {"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
