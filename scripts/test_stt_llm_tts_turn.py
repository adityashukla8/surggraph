"""Real, no-mocks smoke test of the new classic STT -> LLM -> TTS pipeline
(plan_v2 §15), against the LOCAL relay (services/surgbot_service).

Uses agents/surgbot/speech.py::synthesize_speech to generate a REAL spoken
clip of a real question, sends it exactly the way the browser does
(mic_start -> binary PCM frames -> mic_stop), and asserts the full pipeline
round-trips for real: a transcribe_audio tool chip with a non-empty real
transcript, at least one LLM tool call (list_accessible_cases, since the
question asks to list cases), a final real model transcript, a
synthesize_speech tool chip, and a non-empty binary audio frame.

Usage: uv run python3 scripts/test_stt_llm_tts_turn.py
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import wave

import websockets

from agents.surgbot.speech import synthesize_speech

RELAY_URL = "ws://127.0.0.1:8091"


def _wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


async def main() -> None:
    session_id = str(uuid.uuid4())
    uri = f"{RELAY_URL}/surgbot/{session_id}/voice"

    print("Synthesizing a real spoken question via Cloud TTS...")
    question = "Please list the accessible cases to start a review session."
    wav = synthesize_speech(question)
    pcm, rate = _wav_to_pcm(wav)
    print(f"Real audio ready: {len(pcm)} bytes of PCM16 @ {rate}Hz")

    saw_stt_started = False
    saw_stt_finished_transcript = ""
    saw_tool_call = False
    saw_model_transcript = ""
    saw_tts_started = False
    saw_tts_finished = False
    saw_audio_bytes = 0

    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session_start", "case_ids": [], "reviewer_id": "stt-llm-tts-test"}))
        print("session_start sent")

        print("\n>>> sending mic_start + real audio (paced to match real capture speed) + mic_stop")
        t_start = asyncio.get_event_loop().time()
        await ws.send(json.dumps({"type": "mic_start"}))
        # Paced to real wall-clock duration, matching how the real
        # AudioWorklet delivers captured mic audio as the reviewer speaks
        # (plan_v2 §16 depends on this overlap — sending the whole clip as
        # fast as possible defeats the point of streaming and would give a
        # misleadingly pessimistic latency reading).
        chunk_ms = 100
        chunk_size = int(chunk_ms / 1000 * rate) * 2  # 2 bytes/sample, mono
        for i in range(0, len(pcm), chunk_size):
            await ws.send(pcm[i : i + chunk_size])
            await asyncio.sleep(chunk_ms / 1000)
        t_audio_sent = asyncio.get_event_loop().time() - t_start
        await ws.send(json.dumps({"type": "mic_stop"}))
        print(f"    (real clip duration paced over {t_audio_sent:.2f}s, then mic_stop)")

        t_mic_stop = asyncio.get_event_loop().time()
        t_stt_done = None
        t_model_text_done = None
        t_first_audio = None
        t_tts_done = None

        end = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < end:
            remaining = end - asyncio.get_event_loop().time()
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
            except asyncio.TimeoutError:
                break

            if isinstance(msg, (bytes, bytearray)):
                if t_first_audio is None:
                    t_first_audio = asyncio.get_event_loop().time()
                saw_audio_bytes += len(msg)
                print(f"EVENT: <binary audio frame, {len(msg)} bytes>")
                continue

            parsed = json.loads(msg)
            print("EVENT:", parsed.get("type"), str(parsed)[:200])

            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") == "transcribe_audio":
                saw_stt_started = True
            if parsed.get("type") == "transcript_delta" and parsed.get("speaker") == "user":
                saw_stt_finished_transcript = parsed.get("text", "")
                t_stt_done = asyncio.get_event_loop().time()
            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") not in ("transcribe_audio", "synthesize_speech"):
                saw_tool_call = True
            if parsed.get("type") == "transcript_delta" and parsed.get("speaker") == "model":
                saw_model_transcript = parsed.get("text", "")
                t_model_text_done = asyncio.get_event_loop().time()
            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") == "synthesize_speech":
                saw_tts_started = True
            if parsed.get("type") == "tool_call_finished" and parsed.get("call_id") and saw_tts_started and not saw_tts_finished:
                saw_tts_finished = True
                t_tts_done = asyncio.get_event_loop().time()
            if parsed.get("type") == "error":
                print("!! ERROR:", parsed.get("detail"))

        await ws.send(json.dumps({"type": "end_session"}))

    print("\n=== LATENCY BREAKDOWN (from mic_stop) ===")
    if t_stt_done:
        print(f"STT tail wait (mic_stop -> transcript): {t_stt_done - t_mic_stop:.2f}s")
    if t_model_text_done and t_stt_done:
        print(f"LLM (+ tool calls):                     {t_model_text_done - t_stt_done:.2f}s")
    if t_first_audio and t_model_text_done:
        print(f"Time to FIRST audio chunk after reply:  {t_first_audio - t_model_text_done:.2f}s")
    if t_tts_done and t_model_text_done:
        print(f"Total TTS streaming time:               {t_tts_done - t_model_text_done:.2f}s")
    if t_tts_done:
        print(f"TOTAL (mic_stop -> all audio sent):     {t_tts_done - t_mic_stop:.2f}s")

    print("\n=== RESULTS ===")
    print(f"STT started:           {saw_stt_started}")
    print(f"STT transcript (real): {saw_stt_finished_transcript!r}")
    print(f"LLM tool call seen:    {saw_tool_call}")
    print(f"Model transcript:      {saw_model_transcript[:200]!r}")
    print(f"TTS started:           {saw_tts_started}")
    print(f"TTS finished:          {saw_tts_finished}")
    print(f"Audio bytes received:  {saw_audio_bytes}")

    ok = (
        saw_stt_started
        and len(saw_stt_finished_transcript) > 0
        and saw_tool_call
        and len(saw_model_transcript) > 0
        and saw_tts_started
        and saw_tts_finished
        and saw_audio_bytes > 0
    )
    print("\nPASSED" if ok else "\nFAILED — see results above")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
