"""SurgBot backend service — the third Cloud Run service, additive to
services/state_service and services/orchestrator_service (neither of which
this file imports from or writes to).

This service does NOT run the ADK Runner itself — that would mean
re-hosting the agent locally, duplicating what Agent Runtime already does,
and losing Identity/Registry/Observability as automatic side effects of
deployment. Its job on the voice path: hold the browser WebSocket open,
accumulate a push-to-talk audio clip, run it through MedASR (a real,
self-deployed medical-domain ASR model — docs/qa_log.md, 2026-08-28;
agents/surgbot/speech.py::transcribe_audio_medasr, replacing Cloud
Speech-to-Text/Chirp 3 for this path), send the transcript to the
ALREADY-DEPLOYED root agent via a real async_stream_query call, and run
the model's reply through Cloud Text-to-Speech before sending audio back
— a classic STT -> LLM -> TTS pipeline (plan_v2 §15), replacing the
EXPERIMENTAL bidi_stream_query Live API transport this service used to
require.

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
import contextlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

import vertexai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents.surgbot import case_index, feedback, speech, store, subagents
from agents.surgbot.root_agent import TOOL_DISCLOSURE, TOOL_PHASE_MAP
from agents.surgbot.schema import PHASE_LABELS, SurgBotSession
from tools import memory_bank
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
_STT_DISCLOSURE = {"agent_name": "speech_to_text", "model_id": "medasr", "api_surface": "vertex_ai_medasr"}
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

    # Approval is the ONLY point feedback becomes knowledge (plan_v2 §16.3) —
    # unapproved/draft feedback never reaches Memory Bank. This replaces the
    # old single-flat-string write with feedback.process_review_feedback,
    # which routes each item to its real target agent individually; that
    # function already fails soft per-item (a Memory Bank failure is
    # recorded on the FeedbackRecord, never raised), so this endpoint's own
    # response never depends on it succeeding.
    #
    # Also writes ONE review_summary fact under feedback.REVIEW_SUMMARY_SCOPE
    # to preserve Phase 6's existing cross-session pattern review — moved off
    # a per-reviewer scope onto the same institution-wide constant used
    # everywhere else in this feature, so Phase 6 reflects every reviewer's
    # approved sessions, not just whichever reviewer has run the most.
    if result["outcome"] in ("approved", "edited"):
        document = await store.get_review(review_id)
        if document is not None:
            await feedback.process_review_feedback(document)

            fact_parts = [document.case_summary, *document.coaching_notes, *document.disagreements]
            summary_fact = " ".join(p for p in fact_parts if p)[:1000]
            if summary_fact:
                # _synthesis_resource_name() is a plain blocking call — must
                # be resolved INSIDE the worker thread, not as an eagerly-
                # evaluated positional arg (that would block this coroutine's
                # own event loop before to_thread ever schedules anything).
                agent_engine = await asyncio.to_thread(_synthesis_resource_name)
                await asyncio.to_thread(memory_bank.create_memory, summary_fact, feedback.REVIEW_SUMMARY_SCOPE, agent_engine)

    return result


@app.get("/surgbot/reviewers/{reviewer_id}/patterns")
async def get_reviewer_patterns(reviewer_id: str, query: str = "review session patterns") -> dict[str, Any]:
    # reviewer_id stays in the URL/response for API-shape compatibility and
    # provenance in logs, but no longer scopes the Memory Bank query itself —
    # see feedback.REVIEW_SUMMARY_SCOPE's docstring (plan_v2 §16.1c): a
    # per-reviewer scope meant any reviewer besides the heaviest user saw an
    # empty pattern history.
    agent_engine = await asyncio.to_thread(_synthesis_resource_name)
    facts = await asyncio.to_thread(memory_bank.retrieve_memories, feedback.REVIEW_SUMMARY_SCOPE, agent_engine, query)
    return {"reviewer_id": reviewer_id, "memories": facts, "count": len(facts)}


# --- WebSocket voice relay -------------------------------------------------


def _disclosure_for(tool_name: str) -> dict[str, str]:
    return TOOL_DISCLOSURE.get(tool_name, _DEFAULT_DISCLOSURE)


async def _send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_text(json.dumps(payload))
    except RuntimeError:
        # Real production observation (Cloud Run logs, 2026-08-28): a client
        # that disconnects mid-turn (closed tab, network drop, a test script
        # ending early) races with an in-flight send — Starlette raises a
        # plain RuntimeError ("Cannot call send once a close message has
        # been sent"), not a catchable WebSocketDisconnect. This is the
        # expected end of a turn whose client is already gone, not a real
        # failure — logged at INFO, not surfaced as an unhandled exception.
        logger.info("surgbot voice ws: dropped a send — client already disconnected")


# Real user report this session: the model's markdown-formatted text (###
# headers, **bold**, bullet lists) was being narrated literally ("hash hash
# hash", "asterisk asterisk") and shown with the raw syntax still visible in
# the transcript feed — both symptoms of the same root cause (raw markdown
# used unprocessed for both speech and display). Not a full markdown parser
# (no HTML, no rendering) — just enough to turn the syntax this model
# actually produces into clean, readable prose for both purposes at once.
_MD_HRULE_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.MULTILINE)
_MD_HEADER_RE = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_MD_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_MD_NUMBERED_RE = re.compile(r"^[ \t]*\d+\.[ \t]+", re.MULTILINE)
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _strip_markdown_for_speech(text: str) -> str:
    # Real bug caught while testing this against the exact text from a real
    # user report: bullet/numbered markers MUST be stripped before bold/
    # italic — a leading "*  *Mechanism:*" (bullet marker immediately
    # followed by an italic-opening marker) let the italic regex pair the
    # BULLET's own "*" with the italic's OPENING "*" instead of its real
    # closing one, leaving a stray trailing "*" behind ("Mechanism:*").
    # Stripping the bullet marker first removes the ambiguity entirely.
    result = _MD_HRULE_RE.sub("", text)
    result = _MD_HEADER_RE.sub("", result)
    result = _MD_BULLET_RE.sub("", result)
    result = _MD_NUMBERED_RE.sub("", result)
    result = _MD_BOLD_RE.sub(r"\1", result)
    result = _MD_ITALIC_RE.sub(r"\1", result)
    result = _MD_CODE_RE.sub(r"\1", result)
    return _MD_BLANK_RUN_RE.sub("\n\n", result).strip()


async def _send_tool_call_started(
    websocket: WebSocket, state: dict[str, Any], call_id: str, tool_name: str, args_summary: str, disclosure: dict[str, str]
) -> None:
    """Sends tool_call_started AND records it as the session's active call
    — stop_narration (see surgbot_voice's main loop) uses this to close out
    whatever chip is open when a turn gets cancelled, instead of leaving it
    stuck showing "running..." forever."""
    state["active_call_id"] = call_id
    await _send_json(
        websocket,
        {"type": "tool_call_started", "call_id": call_id, "tool_name": tool_name, "args_summary": args_summary, **disclosure},
    )


async def _send_tool_call_finished(websocket: WebSocket, state: dict[str, Any], call_id: str, summary: str) -> None:
    if state.get("active_call_id") == call_id:
        state["active_call_id"] = None
    await _send_json(websocket, {"type": "tool_call_finished", "call_id": call_id, "summary": summary})


async def _send_turn_to_agent(
    websocket: WebSocket, engine, state: dict[str, Any], text: str, *, speak: bool = True
) -> None:
    """Sends one real turn to the deployed root agent via async_stream_query
    and relays every resulting event to the browser — phase_changed and
    tool_call_started/finished exactly as before (same TOOL_PHASE_MAP/
    TOOL_DISCLOSURE lookups, same draft_review_document -> review_document_
    ready special case), just sourced from a plain async_stream_query
    iterator instead of a bidi_stream_query connection's receive() loop.
    Ends with one final transcript_delta (the model's real cumulative
    reply, markdown stripped for both display and speech) and, if
    speak=True, dispatches that reply through Cloud TTS.

    speak=False is the real fix for typed input getting a spoken-and-typed
    response back: a reviewer typing into the text fallback almost
    certainly wants a typed reply, not narration — surgbot_voice's main
    loop passes speak=False for text_turn, speak=True (the default) for a
    real push-to-talk audio turn.

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
                    await _send_tool_call_started(
                        websocket, state, call_id, tool_name, json.dumps(function_call.get("args", {}))[:300], disclosure
                    )

                function_response = part.get("function_response")
                if function_response:
                    call_id = function_response.get("id") or "unknown"
                    tool_name = open_calls.pop(call_id, function_response.get("name", ""))
                    response_body = function_response.get("response", {})
                    summary = json.dumps(response_body)[:300]
                    await _send_tool_call_finished(websocket, state, call_id, summary)

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

    # Real user report this session: raw markdown (### headers, **bold**,
    # bullet lists) was both shown with the literal syntax visible AND
    # narrated literally ("hash hash hash"). One cleaned copy, used for
    # both the displayed transcript and (if speak) the TTS input, so
    # there's no risk of the two drifting apart.
    display_text = _strip_markdown_for_speech(final_text)
    await _send_json(websocket, {"type": "transcript_delta", "speaker": "model", "text": display_text, "final": True})

    if not speak:
        # Real user report this session: typed input got a SPOKEN reply
        # back too — a reviewer using the text fallback almost certainly
        # wants a typed one. No TTS call at all for a text_turn.
        return

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
    # a bookkeeping window elsewhere in this protocol. Cancelling THIS
    # coroutine mid-stream (surgbot_voice's stop_narration handling) simply
    # stops the async for loop from sending any further chunks — the
    # caller is responsible for closing out the tool_call_started chip.
    tts_call_id = f"c-{uuid.uuid4().hex[:8]}"
    await _send_tool_call_started(
        websocket, state, tts_call_id, "synthesize_speech", f"{len(display_text)} chars", _TTS_DISCLOSURE
    )
    total_bytes = 0
    try:
        # Real production bug (Cloud Run logs, 2026-08-28): synthesize_
        # speech_streaming wraps its own yield in an OTel
        # start_as_current_span. Breaking/returning out of this loop early
        # (disconnect, stop_narration cancellation) without explicitly
        # closing the generator left it to Python's garbage collector to
        # call aclose() at some arbitrary later point — in a DIFFERENT
        # asyncio context than the one that opened the span, which OTel's
        # context-detach raises a real ValueError over ("Token ... was
        # created in a different Context"). contextlib.aclosing forces
        # aclose() to run HERE, in this same task/context, the moment the
        # loop exits for any reason.
        async with contextlib.aclosing(speech.synthesize_speech_streaming(display_text)) as chunks:
            async for chunk in chunks:
                total_bytes += len(chunk)
                try:
                    await websocket.send_bytes(chunk)
                except RuntimeError:
                    # Same benign disconnect-mid-stream case _send_json
                    # guards against — stop sending further chunks rather
                    # than raising once per remaining chunk (real observed
                    # behavior: this cascaded into 3 separate unhandled-
                    # exception log entries from one root cause before this
                    # fix).
                    logger.info(
                        "surgbot voice ws[%s]: client disconnected mid-narration, stopping TTS stream",
                        state["session_id"],
                    )
                    return
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: synthesize_speech_streaming failed", state["session_id"])
        await _send_tool_call_finished(websocket, state, tts_call_id, f"error: {exc}")
        await _send_json(websocket, {"type": "error", "detail": f"Text-to-speech failed: {exc}"})
        return
    await _send_tool_call_finished(websocket, state, tts_call_id, f"{total_bytes} bytes")


async def _handle_audio_turn(
    websocket: WebSocket, engine, state: dict[str, Any], audio_bytes: bytes | None
) -> None:
    """Finalizes one push-to-talk turn's transcription via MedASR — a real,
    self-deployed Health AI Developer Foundations Conformer ASR model
    (docs/qa_log.md, 2026-08-28), replacing Cloud Speech-to-Text v2/Chirp 3
    for this real-time voice path. MedASR's deployed container is a batch
    endpoint (no documented streaming route — confirmed, not assumed), so
    unlike the prior StreamingTranscription design, the whole clip is
    accumulated during mic hold (see the binary-frame branch in
    surgbot_voice below) and sent as ONE call here, on mic_stop. Real,
    measured latency once the container is warm: ~1.1-1.2s for an ~8.5s
    clip — faster than Chirp 3's one-shot latency, though it gives up
    Chirp 3's "most of the recognition already happened by mic_stop"
    property streaming had. Discloses exactly like a tool call, then hands
    the real transcript to _send_turn_to_agent."""
    if not audio_bytes:
        # mic_stop with no matching mic_start, or a clip with zero bytes
        # captured (e.g. a stray/duplicate message) — nothing to
        # transcribe, not a real error.
        return

    call_id = f"c-{uuid.uuid4().hex[:8]}"
    logger.info("surgbot voice ws[%s]: transcribe_audio_medasr (%d bytes)", state["session_id"], len(audio_bytes))
    await _send_tool_call_started(
        websocket, state, call_id, "transcribe_audio_medasr", f"{len(audio_bytes)} bytes", _STT_DISCLOSURE
    )
    try:
        transcript = await asyncio.to_thread(speech.transcribe_audio_medasr, audio_bytes, sample_rate_hz=16000)
    except Exception as exc:
        logger.exception("surgbot voice ws[%s]: transcribe_audio_medasr failed", state["session_id"])
        await _send_tool_call_finished(websocket, state, call_id, f"error: {exc}")
        await _send_json(websocket, {"type": "error", "detail": f"Speech-to-text failed: {exc}"})
        return
    logger.info("surgbot voice ws[%s]: transcribe_audio_medasr -> %r", state["session_id"], transcript)
    await _send_tool_call_finished(websocket, state, call_id, transcript[:300])

    if not transcript:
        await _send_json(websocket, {"type": "error", "detail": "No speech detected — press and hold, then try again."})
        return

    await _send_json(websocket, {"type": "transcript_delta", "speaker": "user", "text": transcript, "final": True})
    await _send_turn_to_agent(websocket, engine, state, transcript)


async def _run_turn(coro) -> None:
    """Wraps a turn-processing coroutine so it's safe to run as a
    background asyncio.Task (see surgbot_voice's main loop, plan_v2 §17 —
    stop_narration): swallows a real cancellation quietly (that IS the
    stop-narration path working as intended, not an error) and is a last-
    resort safety net for anything genuinely unexpected escaping the
    turn's own error handling."""
    try:
        await coro
    except asyncio.CancelledError:
        logger.info("surgbot voice ws: turn task cancelled")
    except Exception:
        logger.exception("surgbot voice ws: turn task raised unexpectedly")


async def _cancel_turn_task(state: dict[str, Any]) -> None:
    """Cancels the session's in-flight turn task, if any, and waits for it
    to actually finish unwinding before returning — real correctness
    requirement, not just tidiness: without this wait, a late write from
    the cancelled task could interleave with whatever the caller sends
    next on the SAME WebSocket, since Starlette's WebSocket has no built-in
    protection against two coroutines both calling .send() concurrently."""
    task = state.get("turn_task")
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    state["turn_task"] = None


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
        # The in-flight turn's background task and its currently-open
        # disclosure chip's call_id, if any (plan_v2 §17 — stop_narration).
        "turn_task": None,
        "active_call_id": None,
    }
    # MedASR is a batch endpoint (docs/qa_log.md, 2026-08-28) — the whole
    # clip is accumulated here during mic hold and sent as one
    # transcribe_audio_medasr() call on mic_stop, replacing the prior
    # per-chunk StreamingTranscription design Chirp 3 used.
    mic_buffer: bytearray | None = None

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
                if mic_buffer is not None:
                    mic_buffer.extend(data)
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
                # A fresh turn starting implies abandoning any straggling
                # previous one — same real correctness reason as
                # stop_narration (§_cancel_turn_task's own docstring):
                # never let two turns write to the same socket at once.
                await _cancel_turn_task(state)
                # Fresh accumulator — audio pushed below (in the
                # binary-frame branch) is buffered until mic_stop, then
                # sent to MedASR as one clip (see _handle_audio_turn).
                mic_buffer = bytearray()
            elif msg_type == "mic_stop":
                buffer_to_finish, mic_buffer = mic_buffer, None
                audio_bytes = bytes(buffer_to_finish) if buffer_to_finish is not None else None
                state["turn_task"] = asyncio.create_task(
                    _run_turn(_handle_audio_turn(websocket, engine, state, audio_bytes))
                )
            elif msg_type == "text_turn":
                await _cancel_turn_task(state)
                # speak=False (plan_v2 §17): a reviewer typing into the
                # text fallback almost certainly wants a typed reply back,
                # not narration — real user report this session.
                state["turn_task"] = asyncio.create_task(
                    _run_turn(_send_turn_to_agent(websocket, engine, state, control.get("text", ""), speak=False))
                )
            elif msg_type == "stop_narration":
                # Real user report this session: no way to stop a long
                # narration without ending the whole session. Closes out
                # whatever chip was open (so it doesn't show "running..."
                # forever) and cancels the in-flight turn — the reviewer
                # can immediately start a new one; the session/ADK
                # conversation itself is untouched.
                active_call_id = state.get("active_call_id")
                await _cancel_turn_task(state)
                if active_call_id:
                    await _send_tool_call_finished(websocket, state, active_call_id, "stopped by reviewer")
                logger.info("surgbot voice ws[%s]: stop_narration — cancelled in-flight turn", session_id)
            elif msg_type == "end_session":
                logger.info("surgbot voice ws[%s]: browser sent end_session", session_id)
                await _cancel_turn_task(state)
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
