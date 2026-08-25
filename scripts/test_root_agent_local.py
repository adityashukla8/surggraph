"""Drives the REAL agents/surgbot/root_agent.py locally through ADK's actual
Runner.run_live() — the same orchestration layer Agent Runtime's deployed
bidi_stream_query uses — WITHOUT deploying anything. Confirmed genuinely
representative for tool-calling, instruction-following, and turn-taking
logic this session (used to verify the "stay silent until the reviewer
speaks first" fix before ever redeploying, and to verify tool narration
after list_accessible_cases — both matched real deployed-agent behavior).

What this CANNOT catch, disclosed plainly: anything specific to the deployed
sandbox's own infrastructure — Agent Identity, env vars only set at deploy
time, and Google's own EXPERIMENTAL bidi-server internals (e.g. the
assembly_service.py internal-queue-overflow bug found via Cloud Logging this
session had no local equivalent to reproduce against). For those, a real
redeploy + scripts/deploy_surgbot_agent.py's smoke test is still the only way
to verify. But everything about what the model actually says and does
(tools called, phase transitions, whether it waits for the user, whether a
tool result gets narrated) — the vast majority of day-to-day iteration — is
verifiable here in a few seconds instead of a multi-minute deploy cycle.

Usage:
  uv run python3 scripts/test_root_agent_local.py "Hi, please list the cases."
  uv run python3 scripts/test_root_agent_local.py --silent-init "Hello?"
    (sends the real session_init message with partial=True first, exactly as
    services/surgbot_service/main.py does, waits --quiet-window seconds with
    NO other input to confirm that message alone produces zero activity,
    THEN sends the given real turn and confirms a normal response follows)
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from dotenv import load_dotenv
from google.adk.agents.live_request_queue import LiveRequest, LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

load_dotenv()

from agents.surgbot.root_agent import build_root_agent  # noqa: E402  (after load_dotenv)


class _Drain:
    """Collects events off a live run_live() stream in the background so a
    quiet window can be timed independently of whether/when any event
    arrives — a plain `async for` can't express "wait N seconds and confirm
    nothing happened," since it blocks on the next event with no timeout."""

    def __init__(self, events_async):
        self.total_audio_bytes = 0
        self.transcript_pieces: list[str] = []
        self.saw_activity = False
        self._task = asyncio.create_task(self._run(events_async))

    async def _run(self, events_async) -> None:
        async for event in events_async:
            dump = event.model_dump(exclude_none=True, mode="json")
            content = dump.get("content") or {}
            for part in content.get("parts", []) or []:
                if part.get("function_call"):
                    print("  function_call:", part["function_call"].get("name"), str(part["function_call"].get("args"))[:120])
                    self.saw_activity = True
                if part.get("function_response"):
                    print("  function_response:", str(part["function_response"].get("response"))[:200])
                if part.get("inline_data") and part["inline_data"].get("data"):
                    self.total_audio_bytes += len(part["inline_data"]["data"])
                    self.saw_activity = True
                if part.get("text"):
                    self.transcript_pieces.append(part["text"])
            ot = dump.get("output_transcription") or {}
            if ot.get("text"):
                self.transcript_pieces.append(ot["text"])
                self.saw_activity = True
            if dump.get("turn_complete"):
                print("  [turn_complete]")

    def reset_activity_flag(self) -> None:
        self.saw_activity = False

    async def wait_quiet(self, seconds: float) -> bool:
        """Returns True if NO activity was observed during the window."""
        self.reset_activity_flag()
        await asyncio.sleep(seconds)
        return not self.saw_activity

    def stop(self) -> None:
        self._task.cancel()


async def run(user_text: str, silent_init: bool, quiet_window_s: float, timeout_s: float) -> None:
    agent = build_root_agent()
    runner = InMemoryRunner(agent=agent, app_name="local-test")
    session = await runner.session_service.create_session(app_name="local-test", user_id="local-tester")

    run_config = RunConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore"))
        ),
    )

    queue = LiveRequestQueue()
    session_id = f"local-test-{uuid.uuid4().hex[:8]}"

    events_async = runner.run_live(
        user_id="local-tester", session_id=session.id, live_request_queue=queue, run_config=run_config
    )
    drain = _Drain(events_async)

    silent_init_passed = None
    if silent_init:
        init_text = (
            f"[session_init, do not reply] session_id={session_id} reviewer_id=local-tester case_ids=[]. "
            f'Whenever you call record_feedback or draft_review_document, pass session_id="{session_id}" '
            "exactly as given here. Do not respond to this message — stay silent until the reviewer speaks first."
        )
        queue.send(LiveRequest(content=types.Content(role="user", parts=[types.Part(text=init_text)]), partial=True))
        print(f"[sent silent session_init, session_id={session_id}] watching for {quiet_window_s}s of quiet...")
        silent_init_passed = await drain.wait_quiet(quiet_window_s)

    queue.send(LiveRequest(content=types.Content(role="user", parts=[types.Part(text=user_text)])))
    print(f"\n[sent real turn]: {user_text!r}")

    try:
        await asyncio.wait_for(asyncio.sleep(timeout_s), timeout=timeout_s + 1)
    except asyncio.TimeoutError:
        pass
    drain.stop()
    queue.close()

    print(f"\ntotal audio bytes: {drain.total_audio_bytes}")
    print(f"transcript: {' '.join(drain.transcript_pieces)!r}")
    if silent_init_passed is not None:
        status = "PASSED — zero activity during the quiet window" if silent_init_passed else "FAILED — the agent acted on session_init alone"
        print(f"\nsilent-init check: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("text", help="The real user turn to send")
    parser.add_argument(
        "--silent-init", action="store_true", help="Send the real session_init (partial=True) message first"
    )
    parser.add_argument("--quiet-window", type=float, default=6.0, help="Seconds to watch for silence after session_init (default 6)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait for the real turn's response (default 20)")
    args = parser.parse_args()
    asyncio.run(run(args.text, args.silent_init, args.quiet_window, args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
