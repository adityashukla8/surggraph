"""Real end-to-end SurgBot test — connects to the LOCAL relay
(services/surgbot_service, must already be running: `uv run uvicorn
services.surgbot_service.main:app --port 8091`) over the real WebSocket
protocol, exercising the full path: relay -> deployed root agent -> real
Firestore case data -> real subagent dispatch -> back through the relay.

This is the consolidated, permanent version of several scratchpad scripts
built and used this session to verify (in order): the audio decode fix, the
duplicate-transcript fix, the no-auto-start fix (the --quiet-check below),
the case-retrieval STATE_SERVICE_URL fix, the case-ID narration fix, and the
new get_error_statistics_across_cases tool — all across the REAL deployed
path, not a local ADK Runner mock (see scripts/test_root_agent_local.py for
that faster, more limited alternative, which can't reach real Firestore case
data the same way and can't exercise the relay/deployment layer at all).

Usage:
  # Full default 6-phase conversation (multiple exchanges per phase):
  uv run python3 scripts/test_surgbot_e2e.py

  # Your own turns instead of the default script:
  uv run python3 scripts/test_surgbot_e2e.py \
      --turn "List the cases available." \
      --turn "Load the first one."

  # Skip the silent-start check (faster, if you just want to test turns):
  uv run python3 scripts/test_surgbot_e2e.py --no-quiet-check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import websockets

SURGBOT_SERVICE_URL = os.environ.get("SURGBOT_SERVICE_TEST_URL", "ws://127.0.0.1:8091")

# A real case id from this project's Firestore data, used only as a
# convenient anchor for the default conversation below — the bot resolves
# "the first case" on its own via real list_accessible_cases/load_case_graph
# tool calls either way, this just keeps the default script deterministic.
_DEFAULT_CASE_ID = "case-2f4a872f4b6f"

DEFAULT_TURNS = [
    # Phase 1 — case framing
    f"Hi, let's start a review. Please load the case for {_DEFAULT_CASE_ID}.",
    "Okay, give me a quick summary of what stands out most in this case.",
    # Phase 2 — phase-by-phase walkthrough
    "Let's walk through the case phase by phase, starting with the first one.",
    "I agree with what was flagged for that phase. Let's move to the next phase too.",
    # Phase 3 — error-and-complication review
    "Let's review the needle handling error that was detected. What was the mechanism?",
    "That seems plausible to me. Please note I'd like more emphasis on needle hand-off technique in training.",
    # Phase 4 — proposal-and-divergence review
    "Now let's look at the corrective proposal for that needle handling error. Was it sound?",
    "Agreed, that proposal looks justified. Are there any divergence alerts I should know about?",
    # Phase 5 — synthesis and approval
    "I think we've covered enough. Please draft the review document now.",
    "Read me the case summary and the coaching notes from the draft.",
    # Phase 6 — cross-session pattern review
    "Last thing -- have I shown any patterns across my past review sessions?",
    "Got it, thanks. That's everything for this session.",
    # Cross-case aggregate question, not tied to any one phase
    "What's the most erroneous case out of all the cases, and what's the most common error type?",
]


async def watch_for_quiet(ws, seconds: float) -> bool:
    """Returns True if NO server activity is observed for `seconds` — the
    real regression test for "no auto-start, only respond once spoken to."""
    print(f"\nWatching {seconds:.0f}s for unprompted activity (expect NONE)...")
    end = asyncio.get_event_loop().time() + seconds
    saw_anything = False
    try:
        while asyncio.get_event_loop().time() < end:
            remaining = end - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.05))
            saw_anything = True
            print("  UNEXPECTED:", str(msg)[:150])
    except asyncio.TimeoutError:
        pass
    print(f"quiet window result: saw_anything={saw_anything} (expect False)")
    return not saw_anything


async def send_turn_and_collect(ws, text: str, wait_s: float = 70.0) -> str:
    print(f"\n{'=' * 70}\nUSER: {text}\n{'=' * 70}")
    await ws.send(json.dumps({"type": "text_turn", "text": text}))
    transcript_pieces: list[str] = []
    end = asyncio.get_event_loop().time() + wait_s
    try:
        while asyncio.get_event_loop().time() < end:
            remaining = end - asyncio.get_event_loop().time()
            msg = await asyncio.wait_for(ws.recv(), timeout=max(remaining, 0.1))
            if isinstance(msg, bytes):
                continue  # raw PCM audio frame — not printed, just drained
            parsed = json.loads(msg)
            t = parsed.get("type")
            if t == "transcript_delta" and parsed.get("speaker") == "model" and parsed.get("final"):
                transcript_pieces.append(parsed["text"])
                end = min(end, asyncio.get_event_loop().time() + 6.0)
            elif t == "tool_call_started":
                print(f">> TOOL CALL: {parsed.get('tool_name')} by {parsed.get('agent_name')} "
                      f"({parsed.get('model_id')} / {parsed.get('api_surface')}) args={parsed.get('args_summary')}")
            elif t == "tool_call_finished":
                print(f"<< TOOL RESULT: {(parsed.get('summary') or '')[:250]}")
            elif t == "phase_changed":
                print(f"-- PHASE: {parsed.get('phase')} ({parsed.get('phase_label')})")
            elif t == "review_document_ready":
                print(f"-- REVIEW DOCUMENT READY: review_id={parsed.get('review_id')} "
                      f"status={parsed.get('approval_status')} sections={list((parsed.get('sections') or {}).keys())}")
            elif t == "interrupted":
                print("-- (interrupted signal — in-flight response was canceled by barge-in)")
            elif t == "error":
                print(f"!! ERROR: {parsed.get('detail')}")
    except asyncio.TimeoutError:
        pass
    full_text = " ".join(transcript_pieces) if transcript_pieces else "(no final transcript captured within the wait window)"
    print(f"\nBOT: {full_text}")
    return full_text


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--turn", action="append", dest="turns", help="A user turn to send (repeatable). Overrides the default 6-phase script if given at least once.")
    parser.add_argument("--wait", type=float, default=70.0, help="Seconds to wait for each turn's response (default 70)")
    parser.add_argument("--quiet-window", type=float, default=6.0, help="Seconds to check for silence before the first turn (default 6)")
    parser.add_argument("--no-quiet-check", action="store_true", help="Skip the silent-start check")
    parser.add_argument("--reviewer-id", default="e2e-script-run", help="reviewer_id to use for this session")
    args = parser.parse_args()

    turns = args.turns if args.turns else DEFAULT_TURNS
    session_id = str(uuid.uuid4())
    uri = f"{SURGBOT_SERVICE_URL}/surgbot/{session_id}/voice"
    print(f"Connecting to {uri}")

    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session_start", "case_ids": [], "reviewer_id": args.reviewer_id}))

        if not args.no_quiet_check:
            quiet_ok = await watch_for_quiet(ws, args.quiet_window)
            if not quiet_ok:
                print("\n!! REGRESSION: the agent spoke/acted before you said anything.")

        for turn in turns:
            await send_turn_and_collect(ws, turn, wait_s=args.wait)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
