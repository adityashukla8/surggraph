"""Real calls against the actual Cloud Speech-to-Text v2 / Cloud
Text-to-Speech APIs — no mocks, matching this project's established
philosophy (tests/test_fhir_write_readback.py's own docstring: "these tests
hit the real public HAPI test server... no mocking of the call itself,
that's the point"). A synthesize->transcribe round trip exercises both real
APIs without needing a checked-in audio fixture.
"""

from __future__ import annotations

import pytest

from agents.surgbot.speech import synthesize_speech, transcribe_audio


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
