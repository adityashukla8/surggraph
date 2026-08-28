"""Real end-to-end SurgBot AUDIO-turn test — the one path
scripts/test_surgbot_e2e.py never exercises (it only sends text_turn).
Connects to the LOCAL relay (services/surgbot_service, must already be
running) over the real WebSocket protocol and drives a real mic_start ->
binary PCM16 audio frames -> mic_stop turn, proving the real MedASR swap
(docs/qa_log.md, 2026-08-28; services/surgbot_service/main.py) works
through the actual running relay, not just at the transcribe_audio_medasr
unit-test level.

The "spoken" audio is real synthesized speech (Cloud TTS, via
agents/surgbot/speech.py::synthesize_speech), resampled to 16kHz mono to
match what the browser's AudioWorklet actually captures — no mocks, no
canned transcript, matching this project's established no-mocks testing
philosophy.

Usage:
  uv run python3 scripts/test_surgbot_audio_e2e.py
"""

from __future__ import annotations

import asyncio
import audioop
import io
import json
import os
import uuid
import wave

import websockets

from agents.surgbot.speech import synthesize_speech

SURGBOT_SERVICE_URL = os.environ.get("SURGBOT_SERVICE_TEST_URL", "ws://127.0.0.1:8091")

SPOKEN_TEXT = "Please list the accessible cases to start a review session."


def _synthesize_16k_pcm(text: str) -> bytes:
    wav_bytes = synthesize_speech(text)
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
    return pcm


async def main() -> int:
    session_id = str(uuid.uuid4())
    uri = f"{SURGBOT_SERVICE_URL}/surgbot/{session_id}/voice"
    print(f"Connecting to {uri}")
    print(f"Real spoken text (via Cloud TTS): {SPOKEN_TEXT!r}")

    pcm = _synthesize_16k_pcm(SPOKEN_TEXT)
    print(f"Real 16kHz PCM16 clip: {len(pcm)} bytes (~{len(pcm) / 2 / 16000:.2f}s)")

    events: list[dict] = []
    stt_transcript: str | None = None
    stt_disclosure: dict | None = None
    model_reply_parts: list[str] = []

    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session_start", "case_ids": [], "reviewer_id": "audio-e2e-script"}))
        await ws.send(json.dumps({"type": "mic_start"}))

        chunk_size = 3200  # 100ms @ 16kHz mono PCM16, matches the frontend's real chunking
        for i in range(0, len(pcm), chunk_size):
            await ws.send(pcm[i : i + chunk_size])
            await asyncio.sleep(0.01)

        await ws.send(json.dumps({"type": "mic_stop"}))

        end = asyncio.get_event_loop().time() + 60.0
        try:
            while asyncio.get_event_loop().time() < end:
                remaining = end - asyncio.get_event_loop().time()
                msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
                if isinstance(msg, bytes):
                    continue  # synthesized reply audio — not needed for this check
                parsed = json.loads(msg)
                events.append(parsed)
                t = parsed.get("type")
                if t == "tool_call_started" and parsed.get("tool_name") == "transcribe_audio_medasr":
                    stt_disclosure = {
                        "agent_name": parsed.get("agent_name"),
                        "model_id": parsed.get("model_id"),
                        "api_surface": parsed.get("api_surface"),
                    }
                    print(f">> STT TOOL CALL: {stt_disclosure} args={parsed.get('args_summary')}")
                elif t == "tool_call_finished" and parsed.get("call_id"):
                    print(f"<< TOOL RESULT: {(parsed.get('summary') or '')[:250]}")
                elif t == "transcript_delta":
                    print(f"-- transcript_delta[{parsed.get('speaker')}, final={parsed.get('final')}]: {parsed.get('text')!r}")
                    if parsed.get("speaker") == "user" and parsed.get("final"):
                        stt_transcript = parsed.get("text")
                    if parsed.get("speaker") == "model" and parsed.get("final"):
                        model_reply_parts.append(parsed.get("text") or "")
                        end = min(end, asyncio.get_event_loop().time() + 3.0)
                elif t == "error":
                    print(f"!! ERROR: {parsed.get('detail')}")
        except asyncio.TimeoutError:
            pass

    print("\n" + "=" * 70)
    print("REAL SPOKEN TEXT:      ", SPOKEN_TEXT)
    print("REAL STT TRANSCRIPT:   ", stt_transcript)
    print("REAL STT DISCLOSURE:   ", stt_disclosure)
    print("REAL MODEL REPLY:      ", " ".join(model_reply_parts) or "(none captured)")
    print("=" * 70)

    ok = True
    if stt_disclosure != {"agent_name": "speech_to_text", "model_id": "medasr", "api_surface": "vertex_ai_medasr"}:
        print("!! FAIL: STT disclosure does not show MedASR — the swap may not be live.")
        ok = False
    if not stt_transcript:
        print("!! FAIL: no real transcript came back.")
        ok = False
    else:
        original_words = set(SPOKEN_TEXT.lower().rstrip(".").split())
        transcript_words = set(stt_transcript.lower().rstrip(".").split())
        overlap = len(original_words & transcript_words) / len(original_words)
        print(f"real word overlap: {overlap:.0%}")
        if overlap < 0.6:
            print("!! FAIL: transcript overlap with the real spoken text is too low.")
            ok = False

    print("\nPASSED" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
