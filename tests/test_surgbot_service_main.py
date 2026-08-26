"""Unit tests for the pure-logic pieces of services/surgbot_service/main.py
that don't require a live WebSocket connection — mainly
_strip_markdown_for_speech, the real fix for a real user report (raw
markdown syntax both narrated literally and shown unrendered in the
transcript feed).
"""

from __future__ import annotations

from services.surgbot_service.main import _strip_markdown_for_speech


def test_strips_header_bold_italic_bullets_hrule():
    text = (
        "### 3. Deficient Instrument Control at 1:10 (Phase 3)\n"
        "*  **Complications: Vesicourethral Anastomotic Leakage**\n"
        "    *  *Mechanism:* Poor control while passing the suture.\n"
        "---\n"
    )
    result = _strip_markdown_for_speech(text)
    assert "#" not in result
    assert "*" not in result
    assert "Deficient Instrument Control at 1:10 (Phase 3)" in result
    assert "Complications: Vesicourethral Anastomotic Leakage" in result
    # Real bug caught while writing this test: a bullet marker immediately
    # followed by an italic-opening marker ("*  *Mechanism:*") let the
    # italic regex pair the wrong two asterisks, leaving a stray "*"
    # behind ("Mechanism:*") — this asserts that regression stays fixed.
    assert "Mechanism: Poor control while passing the suture." in result
    assert "Mechanism:*" not in result


def test_strips_numbered_lists_and_code_spans():
    text = "1. First item\n2. Second item\nRun `transcribe_audio()` to start."
    result = _strip_markdown_for_speech(text)
    assert "1." not in result
    assert "`" not in result
    assert "First item" in result
    assert "Run transcribe_audio() to start." in result


def test_collapses_blank_lines_left_by_removed_syntax():
    text = "Paragraph one.\n---\n\n\n\nParagraph two."
    result = _strip_markdown_for_speech(text)
    assert "\n\n\n" not in result
    assert "Paragraph one." in result
    assert "Paragraph two." in result


def test_plain_text_with_no_markdown_is_unchanged():
    text = "The patient tolerated the procedure well and vitals remained stable."
    assert _strip_markdown_for_speech(text) == text


def test_empty_string_stays_empty():
    assert _strip_markdown_for_speech("") == ""
