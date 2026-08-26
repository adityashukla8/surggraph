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

        print("\n>>> sending mic_start + real audio + mic_stop")
        await ws.send(json.dumps({"type": "mic_start"}))
        # Send in reasonably sized chunks, like the browser's worklet would.
        chunk_size = 3200  # 100ms @ 16kHz mono PCM16 -- irrelevant here since
        # this clip is 24kHz, but chunking exercises the same accumulation
        # path the real worklet uses.
        for i in range(0, len(pcm), chunk_size):
            await ws.send(pcm[i : i + chunk_size])
        await ws.send(json.dumps({"type": "mic_stop"}))

        end = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < end:
            remaining = end - asyncio.get_event_loop().time()
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
            except asyncio.TimeoutError:
                break

            if isinstance(msg, (bytes, bytearray)):
                saw_audio_bytes += len(msg)
                print(f"EVENT: <binary audio frame, {len(msg)} bytes>")
                continue

            parsed = json.loads(msg)
            print("EVENT:", parsed.get("type"), str(parsed)[:200])

            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") == "transcribe_audio":
                saw_stt_started = True
            if parsed.get("type") == "tool_call_finished" and "summary" in parsed and not saw_tool_call:
                # First finished call after STT is transcribe_audio's own —
                # capture its transcript from the summary field.
                pass
            if parsed.get("type") == "transcript_delta" and parsed.get("speaker") == "user":
                saw_stt_finished_transcript = parsed.get("text", "")
            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") not in ("transcribe_audio", "synthesize_speech"):
                saw_tool_call = True
            if parsed.get("type") == "transcript_delta" and parsed.get("speaker") == "model":
                saw_model_transcript = parsed.get("text", "")
            if parsed.get("type") == "tool_call_started" and parsed.get("tool_name") == "synthesize_speech":
                saw_tts_started = True
            if parsed.get("type") == "tool_call_finished" and parsed.get("call_id") and saw_tts_started and not saw_tts_finished:
                saw_tts_finished = True
            if parsed.get("type") == "error":
                print("!! ERROR:", parsed.get("detail"))

        await ws.send(json.dumps({"type": "end_session"}))

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
