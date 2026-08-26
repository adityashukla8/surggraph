"""SurgBot backend service — the third Cloud Run service, additive to
services/state_service and services/orchestrator_service (neither of which
this file imports from or writes to).

This service does NOT run the ADK Runner itself — that would mean
re-hosting the agent locally, duplicating what Agent Runtime already does,
and losing Identity/Registry/Observability as automatic side effects of
deployment. Its job on the voice path: hold the browser WebSocket open,
accumulate a push-to-talk audio clip, run it through Cloud Speech-to-Text
(agents/surgbot/speech.py), send the transcript to the ALREADY-DEPLOYED
root agent via a real async_stream_query call, and run the model's reply
through Cloud Text-to-Speech before sending audio back — a classic
STT -> LLM -> TTS pipeline (plan_v2 §15), replacing the EXPERIMENTAL
bidi_stream_query Live API transport this service used to require.

WHY THIS MIGRATION (plan_v2 §15, real production incidents this session,
not a preference): (1) automatic-VAD-triggered barge-in reliably overflowed
a proprietary internal queue in Agent Runtime's own assembly_service.py
(QueueFull on output_queue/application_queue) — push-to-talk mitigated but
did not eliminate this; (2) bidi_stream_query's real ~10-minute session
ceiling was hit live this session (base_llm_flow.py: "Received go away
signal: time_left='57s'"), after which ADK's own auto-reconnect logic hung
for 85s and the whole session was cancelled — with zero client-side lever to
fix it. The STABLE, request/response async_stream_query path this file now
uses is the SAME transport agents/surgbot/subagents.py already uses
successfully for three real deployed subagents — proven, not new.

DISCLOSURE IS STRUCTURAL, NOT COSMETIC (unchanged requirement): every
function_call/function_response part seen in a relayed event becomes a
tool_call_started/tool_call_finished control message carrying a REQUIRED
agent_name + model_id + api_surface triple, resolved from agents/surgbot/
root_agent.py::TOOL_DISCLOSURE. The two new speech stages (transcribe_audio,
synthesize_speech) are represented as the SAME tool_call_started/finished
message shape — real per-invocation disclosure for the whole pipeline, not
just the LLM's own tool calls, with zero new wire message types needed.

SESSION CONTINUITY: one ADK session (engine.async_create_session) is
created ONCE per WebSocket connection and reused across every turn via
async_stream_query(session_id=...) — ADK's own session service carries
conversation history forward automatically (confirmed via a real source
read of vertexai/agent_engines/templates/adk.py::AdkApp.async_stream_query
this session). This replaces the old Live-only "carried-forward running
summary across a bidi reconnect" workaround, which is no longer needed.

LATENCY (plan_v2 §16, real measurement — not touching agent/tool/prompt
code at all): STT and TTS both now stream instead of running one-shot.
Real numbers measured this session: one-shot Recognize on a held clip took
2-11s (highly variable) after release; StreamingRecognize, fed audio as
it's captured, cut the wait AFTER release to ~1.5s since most recognition
work already happened during the hold. One-shot synthesize_speech took
2.6-2.8s before ANY audio could play; streaming_synthesize (sentence-
chunked) gets the first audio chunk out in ~0.2-0.6s, so playback starts
almost immediately instead of after the whole reply is synthesized. The
LLM/tool-call leg (10-27s measured) is the actual dominant cost but is
explicitly out of scope — real, disclosed, not something this pass touches.
"""

from __future__ import annotations

import asyncio
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

from agents.surgbot import case_index, memory_bank, speech, store, subagents
from agents.surgbot.root_agent import TOOL_DISCLOSURE, TOOL_PHASE_MAP
from agents.surgbot.schema import PHASE_LABELS, SurgBotSession
from tools.gemini_model import GEMINI_MODEL
from tools.observability import setup_cloud_observability

load_dotenv()
setup_cloud_observability("surggraph-surgbot-service")

# Real bug found earlier this session: nothing in this app ever called
# logging.basicConfig(), so Python's root logger had NO handler attached at
# all. Every logger.info() call in this module was being silently dropped.
# Root level set to INFO so this app's own logs actually appear;
# known-noisy third-party loggers turned back down explicitly rather than
# silencing INFO globally.
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

_DEFAULT_DISCLOSURE = {"agent_name": "surgbot_root", "model_id": GEMINI_MODEL, "api_surface": "vertex_ai_global"}
_STT_DISCLOSURE = {"agent_name": "speech_to_text", "model_id": "chirp_3", "api_surface": "google_cloud_speech"}
_TTS_DISCLOSURE = {
    "agent_name": "text_to_speech",
    "model_id": f"chirp3-hd-{speech.TTS_VOICE.lower()}",
    "api_surface": "google_cloud_tts",
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

    list_cases() is a plain synchronous Firestore call — this app runs ALL
    concurrent voice WebSocket sessions on one shared event loop, so calling
    it directly here would stall every other browser's live turn too, not
    just this request. Run it in a thread.
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


async def _send_turn_to_agent(websocket: WebSocket, engine, state: dict[str, Any], text: str) -> None:
    """Sends one real turn to the deployed root agent via async_stream_query
    and relays every resulting event to the browser — phase_changed and
    tool_call_started/finished exactly as before (same TOOL_PHASE_MAP/
    TOOL_DISCLOSURE lookups, same draft_review_document -> review_document_
    ready special case), just sourced from a plain async_stream_query
    iterator instead of a bidi_stream_query connection's receive() loop.
    Ends with one final transcript_delta (the model's real cumulative
    reply) and dispatches that reply through Cloud TTS.

    The FIRST real turn of a session gets a one-time bracketed
    `[context: ...]` tag prepended (see root_agent.py's own instruction) —
    ADK's session continuity means the model only needs to see this once.
    """
    if not state.get("context_sent"):
        message = (
            f"[context: session_id={state['session_id']} reviewer_id={state['reviewer_id']} "
            f"case_ids={state['case_ids']}] {text}"
        )
        state["context_sent"] = True
    else:
        message = text

    logger.info("surgbot voice ws[%s]: sending turn to root agent: %r", state["session_id"], message[:200])
    open_calls: dict[str, str] = {}
    final_text = ""
    try:
        async for event in engine.async_stream_query(
            user_id=state["reviewer_id"], session_id=state["agent_session_id"], message=message
        ):
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
                        # Fire-and-forget — session bookkeeping should never
                        # gate how fast we drain the agent's event stream.
                        asyncio.create_task(store.update_session(state["session_id"], current_phase=phase))

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

                # Real, cumulative restatement pattern (same as agents/
                # surgbot/subagents.py::invoke_subagent's own extraction) —
                # the LAST text part seen is the model's full reply, not a
                # fragment to append.
                text_part = part.get("text")
                if text_part:
                    final_text = text_part
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: async_stream_query failed", state["session_id"])
        await _send_json(websocket, {"type": "error", "detail": f"{type(exc).__name__}: {exc}"})
        return

    logger.info("surgbot voice ws[%s]: root agent final_text=%r", state["session_id"], final_text[:200])
    if not final_text:
        logger.warning("surgbot voice ws[%s]: async_stream_query produced no final text — nothing to speak", state["session_id"])
        return

    await _send_json(websocket, {"type": "transcript_delta", "speaker": "model", "text": final_text, "final": True})

    # Streaming TTS (plan_v2 §16): sends each synthesized sentence as its
    # own binary frame as soon as it's ready, instead of waiting for the
    # WHOLE reply to synthesize before sending anything — real measured win,
    # first audio chunk in ~0.2-0.6s vs. 2.6-2.8s for the one-shot call.
    # Frames are raw PCM16 @ speech.TTS_SAMPLE_RATE_HZ (NOT a WAV file —
    # contrast with the one-shot synthesize_speech's output); the frontend
    # decodes them with PcmChunkPlayer, not decodeAudioData. tool_call_
    # finished (sent after the last chunk) doubles as the "no more audio
    # coming for this turn" signal — the frontend uses it to know when to
    # stop waiting for further chunks, same real event already used to end
    # a bookkeeping window elsewhere in this protocol.
    tts_call_id = f"c-{uuid.uuid4().hex[:8]}"
    await _send_json(
        websocket,
        {"type": "tool_call_started", "call_id": tts_call_id, "tool_name": "synthesize_speech", "args_summary": f"{len(final_text)} chars", **_TTS_DISCLOSURE},
    )
    total_bytes = 0
    try:
        async for chunk in speech.synthesize_speech_streaming(final_text):
            total_bytes += len(chunk)
            await websocket.send_bytes(chunk)
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: synthesize_speech_streaming failed", state["session_id"])
        await _send_json(websocket, {"type": "tool_call_finished", "call_id": tts_call_id, "summary": f"error: {exc}"})
        await _send_json(websocket, {"type": "error", "detail": f"Text-to-speech failed: {exc}"})
        return
    await _send_json(websocket, {"type": "tool_call_finished", "call_id": tts_call_id, "summary": f"{total_bytes} bytes"})


async def _handle_audio_turn(
    websocket: WebSocket, engine, state: dict[str, Any], stt_session: speech.StreamingTranscription | None
) -> None:
    """Finalizes one push-to-talk turn's real-time streaming transcription
    (plan_v2 §16 — audio was already being fed to Cloud Speech-to-Text via
    stt_session.push_audio() as it was captured, since mic_start; most of
    the recognition work has typically already happened by the time this
    runs), discloses that stage exactly like a tool call, then hands the
    real transcript to _send_turn_to_agent."""
    if stt_session is None:
        # mic_stop with no matching mic_start (e.g. a stray/duplicate
        # message) — nothing to finalize, not a real error.
        return

    call_id = f"c-{uuid.uuid4().hex[:8]}"
    logger.info(
        "surgbot voice ws[%s]: finalizing streaming transcribe_audio (%d bytes pushed)",
        state["session_id"], stt_session.bytes_pushed,
    )
    await _send_json(
        websocket,
        {
            "type": "tool_call_started",
            "call_id": call_id,
            "tool_name": "transcribe_audio",
            "args_summary": f"streaming, {stt_session.bytes_pushed} bytes",
            **_STT_DISCLOSURE,
        },
    )
    try:
        transcript = await stt_session.finish()
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: streaming transcribe_audio failed", state["session_id"])
        await _send_json(websocket, {"type": "tool_call_finished", "call_id": call_id, "summary": f"error: {exc}"})
        await _send_json(websocket, {"type": "error", "detail": f"Speech-to-text failed: {exc}"})
        return
    logger.info("surgbot voice ws[%s]: transcribe_audio -> %r", state["session_id"], transcript)
    await _send_json(websocket, {"type": "tool_call_finished", "call_id": call_id, "summary": transcript[:300]})

    if not transcript:
        await _send_json(websocket, {"type": "error", "detail": "No speech detected — press and hold, then try again."})
        return

    await _send_json(websocket, {"type": "transcript_delta", "speaker": "user", "text": transcript, "final": True})
    await _send_turn_to_agent(websocket, engine, state, transcript)


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

    session = SurgBotSession(session_id=session_id, case_ids=case_ids, reviewer_id=reviewer_id, reasoning_model_id=GEMINI_MODEL)
    await store.create_session(session)

    client = _get_vertex_client()
    try:
        engine = client.agent_engines.get(name=resource_name)
        agent_session = await engine.async_create_session(user_id=reviewer_id)
        agent_session_id = agent_session.get("id") if isinstance(agent_session, dict) else agent_session.id
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: failed to resolve/open a session on the deployed root agent", session_id)
        await _send_json(websocket, {"type": "error", "detail": f"Could not reach SurgBot: {type(exc).__name__}: {exc}"})
        await websocket.close()
        return

    state: dict[str, Any] = {
        "session_id": session_id,
        "reviewer_id": reviewer_id,
        "case_ids": case_ids,
        "agent_session_id": agent_session_id,
        "current_phase": None,
        "context_sent": False,
    }
    # Real streaming Speech-to-Text turn (plan_v2 §16) — created fresh on
    # each mic_start, fed every captured chunk AS IT ARRIVES (not
    # accumulated and sent as one blob on release), finalized on mic_stop.
    stt_session: speech.StreamingTranscription | None = None

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                logger.info(
                    "surgbot voice ws[%s]: browser disconnected (code=%r, reason=%r)",
                    session_id, message.get("code"), message.get("reason"),
                )
                break

            data = message.get("bytes")
            if data is not None:
                if stt_session is not None:
                    stt_session.push_audio(bytes(data))
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
            if msg_type == "mic_start":
                # Real StreamingRecognize session, started immediately —
                # audio pushed below (in the binary-frame branch) starts
                # reaching Cloud Speech-to-Text while the reviewer is still
                # talking, not only after they release (plan_v2 §16).
                stt_session = speech.StreamingTranscription(sample_rate_hz=16000)
                stt_session.start()
            elif msg_type == "mic_stop":
                session_to_finish, stt_session = stt_session, None
                await _handle_audio_turn(websocket, engine, state, session_to_finish)
            elif msg_type == "text_turn":
                await _send_turn_to_agent(websocket, engine, state, control.get("text", ""))
            elif msg_type == "end_session":
                logger.info("surgbot voice ws[%s]: browser sent end_session", session_id)
                break
            elif msg_type == "session_start":
                # Already consumed before this loop starts; a duplicate is
                # harmless to ignore.
                continue
            else:
                logger.warning("surgbot voice ws: unrecognized control message type %r", msg_type)
    except Exception:
        logger.exception("surgbot voice ws[%s]: main loop raised", session_id)
        try:
            await _send_json(websocket, {"type": "error", "detail": "SurgBot service hit an internal error"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
