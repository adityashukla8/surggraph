"""Real calls against the actual Cloud Speech-to-Text v2 / Cloud
Text-to-Speech APIs — no mocks, matching this project's established
philosophy (tests/test_fhir_write_readback.py's own docstring: "these tests
hit the real public HAPI test server... no mocking of the call itself,
that's the point"). A synthesize->transcribe round trip exercises both real
APIs without needing a checked-in audio fixture.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.surgbot.speech import (
    StreamingTranscription,
    synthesize_speech,
    synthesize_speech_streaming,
    transcribe_audio,
    transcribe_audio_medasr,
)


def _wav_pcm_and_rate(wav_bytes: bytes) -> tuple[bytes, int]:
    import io
    import wave

    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def test_synthesize_then_transcribe_round_trip():
    original = "The patient tolerated the procedure well and vitals remained stable."
    audio = synthesize_speech(original)
    assert audio[:4] == b"RIFF", "Chirp 3 HD LINEAR16 output should be a real WAV file"

    pcm, rate = _wav_pcm_and_rate(audio)
    transcript = transcribe_audio(pcm, sample_rate_hz=rate)

    original_words = set(original.lower().rstrip(".").split())
    transcript_words = set(transcript.lower().rstrip(".").split())
    overlap = original_words & transcript_words
    assert len(overlap) / len(original_words) >= 0.8, (
        f"expected most words to round-trip; original={original!r} transcript={transcript!r}"
    )


def test_transcribe_empty_audio_returns_empty_string_not_an_error():
    assert transcribe_audio(b"", sample_rate_hz=16000) == ""


def test_transcribe_silence_returns_clean_empty_result():
    silence = bytes(3200)  # 100ms of 16kHz mono PCM16 silence
    assert transcribe_audio(silence, sample_rate_hz=16000) == ""


def test_synthesize_empty_text_raises():
    with pytest.raises(ValueError):
        synthesize_speech("")
    with pytest.raises(ValueError):
        synthesize_speech("   ")


@pytest.mark.asyncio
async def test_streaming_transcription_round_trip_via_synthesized_clip():
    """Real StreamingRecognize call — feeds a real synthesized clip in as
    chunks (mirroring how the relay feeds real captured mic audio), same
    round-trip philosophy as the one-shot test above."""
    original = "The patient tolerated the procedure well and vitals remained stable."
    wav = synthesize_speech(original)
    pcm, rate = _wav_pcm_and_rate(wav)

    session = StreamingTranscription(sample_rate_hz=rate)
    session.start()
    chunk_size = 3200
    for i in range(0, len(pcm), chunk_size):
        session.push_audio(pcm[i : i + chunk_size])
        await asyncio.sleep(0.01)
    transcript = await session.finish()

    original_words = set(original.lower().rstrip(".").split())
    transcript_words = set(transcript.lower().rstrip(".").split())
    overlap = original_words & transcript_words
    assert len(overlap) / len(original_words) >= 0.8, (
        f"expected most words to round-trip; original={original!r} transcript={transcript!r}"
    )
    assert session.bytes_pushed == len(pcm)


@pytest.mark.asyncio
async def test_streaming_transcription_finish_without_start_returns_empty_not_an_error():
    session = StreamingTranscription()
    assert await session.finish() == ""


@pytest.mark.asyncio
async def test_streaming_transcription_no_audio_returns_clean_empty_result():
    session = StreamingTranscription(sample_rate_hz=16000)
    session.start()
    transcript = await session.finish()
    assert transcript == ""


@pytest.mark.asyncio
async def test_synthesize_speech_streaming_yields_multiple_real_chunks():
    text = (
        "Hello! I am SurgBot, your conversational assistant for cross-case surgical reviews. "
        "I can guide you through a structured review of your completed cases."
    )
    chunks = [chunk async for chunk in synthesize_speech_streaming(text)]
    assert len(chunks) > 1, "a two-sentence reply should stream as more than one chunk"
    assert all(len(c) > 0 for c in chunks)
    # Streaming output is raw PCM (no WAV header) — a real, confirmed
    # difference from synthesize_speech's WAV output (see module docstring).
    assert chunks[0][:4] != b"RIFF"


@pytest.mark.asyncio
async def test_synthesize_speech_streaming_empty_text_raises():
    with pytest.raises(ValueError):
        async for _ in synthesize_speech_streaming(""):
            pass


# --- MedASR — real, self-deployed endpoint (agents/surgbot/speech.py) -------
#
# Same round-trip philosophy as Chirp 3 above, but with a real sentence
# drawn from this project's own actual demo vocabulary (needle_handling /
# vesicourethral anastomosis / laparoscopic) rather than a generic sentence
# — MedASR's whole real value proposition is medical-terminology accuracy,
# so the test should actually exercise that, not just prove connectivity.


def test_medasr_synthesize_then_transcribe_round_trip():
    import audioop

    original = (
        "The needle was lost during the vesicourethral anastomosis and retrieved after a brief "
        "laparoscopic search of the abdominal wall."
    )
    audio = synthesize_speech(original)
    pcm, rate = _wav_pcm_and_rate(audio)

    # MedASR's real deployed container is strict about exactly 16kHz mono
    # (confirmed via a real 400: "Sample rate 24000 is not 16000") — the
    # actual production capture path (browser AudioWorklet) is already
    # 16kHz-native, so this resample only exists to make synthesize_speech's
    # 24kHz TTS output usable as this test's synthetic input.
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
        rate = 16000

    transcript = transcribe_audio_medasr(pcm, sample_rate_hz=rate)

    original_words = set(original.lower().rstrip(".").split())
    transcript_words = set(transcript.lower().rstrip(".").split())
    overlap = original_words & transcript_words
    assert len(overlap) / len(original_words) >= 0.6, (
        f"expected most words to round-trip; original={original!r} transcript={transcript!r}"
    )


def test_medasr_transcribe_empty_audio_returns_empty_string_not_an_error():
    assert transcribe_audio_medasr(b"", sample_rate_hz=16000) == ""
