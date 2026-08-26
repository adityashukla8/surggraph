"""Classic speech I/O for SurgBot — Cloud Speech-to-Text (STT) and Cloud
Text-to-Speech (TTS), replacing the Live API's integrated audio pipeline
(plan_v2 §15: real, repeated Live API/bidi_stream_query crashes — a
proprietary internal queue overflow on barge-in, and a real ~10-minute
session ceiling hit live this session with a failed auto-reconnect — led to
this migration). New file, mirrors agents/surgbot/model_armor.py's own
lazy-singleton-client pattern; nothing else in this codebase does voice I/O,
so this is its own concern, not an edit to any shared tools/ module.

STT: Cloud Speech-to-Text v2, Chirp 3 model, synchronous `Recognize` — the
documented method for audio under a minute, exactly a push-to-talk clip
(docs.cloud.google.com/speech-to-text/docs/models/chirp-3). Chirp 3 is GA
only in the `us`/`eu` MULTI-region — a genuinely different region value than
this project's usual zonal `us-central1`, confirmed via docs and via a real
recognize call in tests/test_surgbot_speech.py, not assumed.

TTS: Cloud Text-to-Speech, Chirp 3 HD voices — call shape confirmed against
real, working sample code pulled directly from Google's own
GoogleCloudPlatform/generative-ai repo this session (audio/speech/
getting-started/get_started_with_chirp_3_hd_voices.ipynb), including its
real `TTS_LOCATION = "global"` default (no regional endpoint needed).
Chirp 3 HD's voice-name catalogue is the SAME one already used for the old
Live API voice (Kore, Puck, Charon, ...) — SURGBOT_TTS_VOICE defaults to
"Kore" for real continuity with the persona already chosen.

Both client calls (recognize, synthesize_speech) are plain synchronous,
blocking gRPC calls — every call site (services/surgbot_service/main.py)
MUST wrap these in asyncio.to_thread, the same class of bug found and fixed
three times elsewhere in agents/surgbot/ this session (subagent lookup,
Model Armor, Memory Bank all had this same bug).

Wrapped in real OTel spans (tools/observability.py's setup_cloud_
observability already configures the process-global TracerProvider these
attach to, same pattern as tools/model_armor.py's own span usage) — ADK's
GenAI auto-instrumentation covers the LLM/tool leg of a turn but has zero
visibility into these two calls, so without this, half of a real per-turn
trace would simply be missing from Cloud Trace.

FAILURE HANDLING: a real STT/TTS API failure is RAISED, never silently
swallowed into an empty/fabricated result — this project's standing rule
(no fake fallback data) applies here exactly as it does everywhere else.
The one legitimate "" transcript case is a genuine, successful recognize
call that simply found no speech (the reviewer held the button and said
nothing) — that's a real, honest empty result, not a failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
from collections.abc import AsyncIterator, Iterator

from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import GoogleAPIError
from google.cloud import texttospeech_v1beta1 as texttospeech
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from opentelemetry import trace

load_dotenv()

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
# Chirp 3 STT is GA only in the `us`/`eu` multi-region (confirmed via docs
# this session) — deliberately NOT this project's usual zonal us-central1.
SPEECH_REGION = os.environ.get("SURGBOT_SPEECH_REGION", "us")
# Chirp 3 HD TTS's own official sample uses "global" (no regional endpoint
# needed) — mirrored here rather than assumed to match STT's region.
TTS_REGION = os.environ.get("SURGBOT_TTS_REGION", "global")
SPEECH_LANGUAGE_CODE = os.environ.get("SURGBOT_SPEECH_LANGUAGE_CODE", "en-US")
# Same voice catalogue the old Live API voice used (agents/surgbot/
# live_model.py, removed by this migration) — "Kore" carries the same
# persona forward.
TTS_VOICE = os.environ.get("SURGBOT_TTS_VOICE", "Kore")
# Arbitrary but fixed — the frontend decodes via AudioContext.decodeAudioData
# (reads the real sample rate out of the WAV header), never hardcodes this.
TTS_SAMPLE_RATE_HZ = 24000

_RECOGNIZER = f"projects/{PROJECT_ID}/locations/{SPEECH_REGION}/recognizers/_"

_speech_client: SpeechClient | None = None
_tts_client: texttospeech.TextToSpeechClient | None = None


def _get_speech_client() -> SpeechClient:
    global _speech_client
    if _speech_client is None:
        endpoint = f"{SPEECH_REGION}-speech.googleapis.com" if SPEECH_REGION != "global" else "speech.googleapis.com"
        _speech_client = SpeechClient(client_options=ClientOptions(api_endpoint=endpoint))
    return _speech_client


def _get_tts_client() -> texttospeech.TextToSpeechClient:
    global _tts_client
    if _tts_client is None:
        endpoint = f"{TTS_REGION}-texttospeech.googleapis.com" if TTS_REGION != "global" else "texttospeech.googleapis.com"
        _tts_client = texttospeech.TextToSpeechClient(client_options=ClientOptions(api_endpoint=endpoint))
    return _tts_client


def transcribe_audio(pcm16_bytes: bytes, sample_rate_hz: int = 16000) -> str:
    """Synchronous Cloud Speech-to-Text v2 Recognize call on one short
    (<1 min) push-to-talk clip. Blocking — callers MUST wrap in
    asyncio.to_thread. Raises on a real API failure; returns "" only for a
    genuine, successful-but-empty recognition (see module docstring)."""
    with _tracer.start_as_current_span("stt.recognize") as span:
        span.set_attribute("stt.model", "chirp_3")
        span.set_attribute("stt.region", SPEECH_REGION)
        span.set_attribute("stt.audio_bytes", len(pcm16_bytes))
        span.set_attribute("stt.audio_duration_s", len(pcm16_bytes) / 2 / sample_rate_hz if pcm16_bytes else 0.0)

        if not pcm16_bytes:
            span.set_attribute("stt.transcript_length", 0)
            return ""

        config = cloud_speech.RecognitionConfig(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=sample_rate_hz,
                audio_channel_count=1,
            ),
            language_codes=[SPEECH_LANGUAGE_CODE],
            model="chirp_3",
        )
        request = cloud_speech.RecognizeRequest(recognizer=_RECOGNIZER, config=config, content=pcm16_bytes)

        try:
            response = _get_speech_client().recognize(request=request)
        except GoogleAPIError as exc:
            logger.exception("surgbot speech: transcribe_audio call failed")
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, f"recognize failed: {exc}"))
            raise

        transcript = " ".join(
            result.alternatives[0].transcript for result in response.results if result.alternatives
        ).strip()
        span.set_attribute("stt.transcript_length", len(transcript))
        return transcript


def synthesize_speech(text: str, voice_name: str = TTS_VOICE) -> bytes:
    """Synchronous Cloud Text-to-Speech Chirp 3 HD synthesize_speech call.
    Returns the raw audio_content bytes (LINEAR16 -> a complete WAV file;
    confirmed in tests/test_surgbot_speech.py, not assumed). Blocking —
    callers MUST wrap in asyncio.to_thread. Raises on a real API failure or
    empty text — never silently returns empty/fabricated audio."""
    if not text.strip():
        raise ValueError("synthesize_speech: text must be non-empty")

    with _tracer.start_as_current_span("tts.synthesize_speech") as span:
        span.set_attribute("tts.voice", voice_name)
        span.set_attribute("tts.text_length", len(text))

        voice = texttospeech.VoiceSelectionParams(
            name=f"{SPEECH_LANGUAGE_CODE}-Chirp3-HD-{voice_name}",
            language_code=SPEECH_LANGUAGE_CODE,
        )
        try:
            response = _get_tts_client().synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=voice,
                audio_config=texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                    sample_rate_hertz=TTS_SAMPLE_RATE_HZ,
                ),
            )
        except GoogleAPIError as exc:
            logger.exception("surgbot speech: synthesize_speech call failed")
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, f"synthesize_speech failed: {exc}"))
            raise

        span.set_attribute("tts.audio_bytes", len(response.audio_content))
        return response.audio_content


# --- Streaming variants (real latency win, plan_v2 §16) ------------------------
#
# The one-shot functions above wait for a COMPLETE input (the whole held
# clip, or the whole LLM reply) before doing any work — measured this
# session: 2-11s for STT on a few seconds of speech, 2.6-2.8s for TTS on a
# short reply (proportionally longer for the 500-700+ char replies real
# conversations produce). Neither number is really "STT/TTS is slow" so
# much as "we throw away the free latency-hiding a stream gives us." These
# streaming variants restructure ONLY the speech I/O transport — how audio
# gets to/from Google's APIs — not anything about what the agent does.
#
# STT: real Cloud Speech-to-Text v2 StreamingRecognize (bidi gRPC) — the
# caller pushes audio chunks in AS THE REVIEWER IS STILL TALKING, so most
# of the recognition work overlaps their own hold duration; by the time
# they release, only a short tail remains to process. AudioEncoding.LINEAR16
# (used by the one-shot Recognize call above) is accepted here too — the
# streaming path's real constraint is different (see TTS below, which does
# reject LINEAR16 for streaming — confirmed empirically, not assumed).
#
# TTS: real Cloud Text-to-Speech streaming_synthesize, sentence-chunked —
# same technique as Google's own official sample (GoogleCloudPlatform/
# generative-ai's Chirp 3 HD getting-started notebook, read directly this
# session). Real, empirically confirmed difference from the one-shot call:
# streaming_synthesize returns RAW PCM chunks with NO WAV header (the
# one-shot synthesize_speech's audio_content IS a full WAV file — these are
# genuinely different response shapes, not a smaller version of the same
# thing) and REJECTS AudioEncoding.LINEAR16 outright ("400 Unsupported audio
# encoding" — a real error hit and diagnosed this session); the correct
# streaming encoding is AudioEncoding.PCM. Measured this session: first
# audio chunk in ~0.6s vs. 2.6s+ for the whole clip via the one-shot call.


def _stt_streaming_requests(
    audio_queue: "queue.Queue[bytes | None]", sample_rate_hz: int
) -> Iterator[cloud_speech.StreamingRecognizeRequest]:
    config = cloud_speech.StreamingRecognitionConfig(
        config=cloud_speech.RecognitionConfig(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=sample_rate_hz,
                audio_channel_count=1,
            ),
            language_codes=[SPEECH_LANGUAGE_CODE],
            model="chirp_3",
        ),
        # No VAD/endpointing here on purpose: push-to-talk already owns turn
        # boundaries (the reviewer's own press/release) — auto-endpointing
        # would risk finalizing early on a mid-thought pause. interim_results
        # stays False; only real, stable final segments are collected.
        streaming_features=cloud_speech.StreamingRecognitionFeatures(interim_results=False),
    )
    yield cloud_speech.StreamingRecognizeRequest(recognizer=_RECOGNIZER, streaming_config=config)
    while True:
        chunk = audio_queue.get()
        if chunk is None:
            return
        yield cloud_speech.StreamingRecognizeRequest(audio=chunk)


def _transcribe_streaming_worker(audio_queue: "queue.Queue[bytes | None]", sample_rate_hz: int) -> str:
    """Drives SpeechClient.streaming_recognize — a blocking, synchronous
    bidi-streaming gRPC call — to completion. Must run in its own thread
    (asyncio.to_thread); see StreamingTranscription below for the async
    wrapper callers actually use."""
    with _tracer.start_as_current_span("stt.streaming_recognize") as span:
        span.set_attribute("stt.model", "chirp_3")
        span.set_attribute("stt.region", SPEECH_REGION)
        transcript_parts: list[str] = []
        try:
            for response in _get_speech_client().streaming_recognize(
                requests=_stt_streaming_requests(audio_queue, sample_rate_hz)
            ):
                for result in response.results:
                    if result.is_final and result.alternatives:
                        transcript_parts.append(result.alternatives[0].transcript)
        except GoogleAPIError as exc:
            logger.exception("surgbot speech: streaming_recognize call failed")
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, f"streaming_recognize failed: {exc}"))
            raise
        transcript = " ".join(p for p in transcript_parts if p).strip()
        span.set_attribute("stt.transcript_length", len(transcript))
        return transcript


class StreamingTranscription:
    """A live streaming Speech-to-Text turn: start() once (typically on
    mic_start), push_audio() for each captured chunk while the reviewer
    holds the button, finish() once (on mic_stop) to get the transcript.
    The real recognize call runs on a background thread the whole time —
    by the time finish() is awaited, most of the audio has usually already
    been processed, so the remaining wait is typically short, not the full
    STT latency of a one-shot call on the same clip."""

    def __init__(self, sample_rate_hz: int = 16000) -> None:
        self._queue: "queue.Queue[bytes | None]" = queue.Queue()
        self._sample_rate_hz = sample_rate_hz
        self._task: asyncio.Task[str] | None = None
        self.bytes_pushed = 0

    def start(self) -> None:
        self._task = asyncio.create_task(
            asyncio.to_thread(_transcribe_streaming_worker, self._queue, self._sample_rate_hz)
        )

    def push_audio(self, chunk: bytes) -> None:
        if chunk:
            self.bytes_pushed += len(chunk)
            self._queue.put(chunk)

    async def finish(self) -> str:
        """Signals end-of-audio and awaits the real transcript. Returns ""
        if start() was never called (no turn in progress) — never raises
        for that case, since it's a caller-sequencing question, not a
        transcription failure."""
        if self._task is None:
            return ""
        self._queue.put(None)  # sentinel: no more audio, finalize
        return await self._task


_SENTENCE_SPLIT_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)|[^.!?]+$")


def _split_into_sentences(text: str) -> list[str]:
    """Splits text into sentence-ish chunks for streaming TTS — same real
    technique as Google's own Chirp 3 HD streaming sample (text_generator
    in the notebook cited above), adapted to return a list. Never sends a
    chunk smaller than a real clause: Cloud TTS's own streaming guidance is
    that a too-small first chunk starves the model of the context it needs
    for natural rhythm/inflection."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.findall(text) if s.strip()]
    return sentences or ([text.strip()] if text.strip() else [])


def _synthesize_streaming_worker(text: str, voice_name: str, chunk_queue: "queue.Queue[bytes | None]") -> None:
    """Drives texttospeech.streaming_synthesize — a blocking, synchronous
    bidi-streaming gRPC call — pushing each raw PCM audio chunk into
    chunk_queue as it's produced, ending with a None sentinel. Must run in
    its own thread; see synthesize_speech_streaming below for the async
    wrapper callers actually use."""
    voice = texttospeech.VoiceSelectionParams(
        name=f"{SPEECH_LANGUAGE_CODE}-Chirp3-HD-{voice_name}",
        language_code=SPEECH_LANGUAGE_CODE,
    )
    config_request = texttospeech.StreamingSynthesizeRequest(
        streaming_config=texttospeech.StreamingSynthesizeConfig(
            voice=voice,
            # PCM, not LINEAR16 — streaming_synthesize rejects LINEAR16
            # outright (confirmed empirically this session: a real 400
            # "Unsupported audio encoding" error). Same TTS_SAMPLE_RATE_HZ
            # as the one-shot call for a consistent, known output rate.
            streaming_audio_config=texttospeech.StreamingAudioConfig(
                audio_encoding=texttospeech.AudioEncoding.PCM,
                sample_rate_hertz=TTS_SAMPLE_RATE_HZ,
            ),
        )
    )

    def request_generator() -> Iterator[texttospeech.StreamingSynthesizeRequest]:
        yield config_request
        for sentence in _split_into_sentences(text):
            yield texttospeech.StreamingSynthesizeRequest(input=texttospeech.StreamingSynthesisInput(text=sentence))

    try:
        for response in _get_tts_client().streaming_synthesize(request_generator()):
            if response.audio_content:
                chunk_queue.put(response.audio_content)
    finally:
        chunk_queue.put(None)


async def synthesize_speech_streaming(text: str, voice_name: str = TTS_VOICE) -> AsyncIterator[bytes]:
    """Streams synthesized audio as a sequence of raw PCM16 chunks (roughly
    one per sentence) instead of returning one complete WAV — real latency
    win for long replies: the FIRST chunk is typically ready in well under
    a second (measured this session: ~0.6s), and playback of it can start
    immediately while later sentences are still being synthesized, instead
    of waiting for the entire reply. Each chunk is raw PCM16 @
    TTS_SAMPLE_RATE_HZ mono, NOT a WAV file — callers must decode/play it
    accordingly (contrast with synthesize_speech's WAV output above)."""
    if not text.strip():
        raise ValueError("synthesize_speech_streaming: text must be non-empty")

    with _tracer.start_as_current_span("tts.streaming_synthesize") as span:
        span.set_attribute("tts.voice", voice_name)
        span.set_attribute("tts.text_length", len(text))
        chunk_queue: "queue.Queue[bytes | None]" = queue.Queue()
        worker_task = asyncio.create_task(
            asyncio.to_thread(_synthesize_streaming_worker, text, voice_name, chunk_queue)
        )
        total_bytes = 0
        try:
            while True:
                chunk = await asyncio.to_thread(chunk_queue.get)
                if chunk is None:
                    break
                total_bytes += len(chunk)
                yield chunk
        finally:
            span.set_attribute("tts.audio_bytes", total_bytes)
            await worker_task
