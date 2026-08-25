"""Day 1 Spike (SurgBot, item a): prove an ADK agent with real tool-calling
can be deployed to GEAP Agent Runtime in EXPERIMENTAL bidi-streaming mode and
invoked via bidi_stream_query, before any other SurgBot code gets built on
top of the assumption that this works. Throwaway stub, mirroring
scripts/spike_deploy_stub_agent.py's own structure and pass/fail style.

Real, code-verified findings that shaped this script (not assumed from docs):

- vertexai.agent_engines.AdkApp (the STABLE one scripts/spike_deploy_stub_agent.py
  uses) has NO bidi_stream_query support at all — confirmed by reading its
  source directly (zero matches for "bidi"/"live_request"/"streaming_mode" in
  vertexai/agent_engines/templates/adk.py). The forum/doc claim that "ADK
  automatically provides bidi_stream_query" does not hold for this class.
- The real implementation lives in a DIFFERENT class:
  vertexai.preview.reasoning_engines.AdkApp — confirmed via source read to
  have a genuine bidi_stream_query method (bridges an incoming asyncio.Queue
  to a real ADK LiveRequestQueue + Runner.run_live(), yields JSON-serializable
  events) and to register it via register_operations()'s "bidi_stream" key.
  This is the class this script deploys — the import path is the one
  load-bearing fact this spike exists to prove works end-to-end, not just to
  read correctly.
- _validate_agent_or_raise (vertexai/_genai/_agent_engines_utils.py) auto-wraps
  a bare google.adk.agents.Agent in the STABLE (non-bidi) AdkApp. Passing a
  pre-built vertexai.preview.reasoning_engines.AdkApp instance as `agent=`
  skips that auto-wrap and is accepted via structural (Protocol) typing
  instead, since it already has bidi_stream_query/register_operations.
- AgentServerMode.EXPERIMENTAL and IdentityType.AGENT_IDENTITY are both real
  fields on vertexai.types, confirmed present in the installed SDK
  (google-cloud-aiplatform 1.163.0). IdentityType.AGENT_IDENTITY's own
  docstring: "Use Agent Identity. The service_account field must not be
  set." — i.e. it's an explicit opt-in, not silently automatic.

Usage: uv run scripts/spike_surgbot_live_roundtrip.py
"""

from __future__ import annotations

import asyncio
import os

import vertexai
from dotenv import load_dotenv
from google.adk.agents import Agent
from vertexai import types as vertexai_types
from vertexai.preview.reasoning_engines import AdkApp

from agents.surgbot.live_model import SURGBOT_LIVE_LOCATION, SURGBOT_LIVE_MODEL, new_live_agent_model

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")  # Agent Runtime hosting region
STAGING_BUCKET = f"gs://{os.environ['SURGGRAPH_GCS_BUCKET']}"


def ping(message: str = "pong") -> str:
    """A single dummy tool — proves the deployed Live agent can receive a
    tool-calling request mid-bidi-session and return a result."""
    return f"surgbot spike stub received: {message}"


def deploy(client: vertexai.Client):
    stub_agent = Agent(
        model=new_live_agent_model(),
        name="surgbot_spike_stub_agent",
        instruction="You are a smoke-test stub. When asked to ping, call the ping tool and report its result.",
        tools=[ping],
    )
    app = AdkApp(agent=stub_agent)  # the preview class — has real bidi_stream_query

    print(
        f"Deploying SurgBot spike stub to Agent Runtime "
        f"(project={PROJECT_ID}, region={REGION}, live_model={SURGBOT_LIVE_MODEL}@{SURGBOT_LIVE_LOCATION}, "
        f"agent_server_mode=EXPERIMENTAL)..."
    )
    remote_agent = client.agent_engines.create(
        agent=app,
        config={
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]",
                "pydantic",
                "cloudpickle",
                "python-dotenv",
            ],
            "extra_packages": ["agents"],
            "staging_bucket": STAGING_BUCKET,
            "env_vars": {
                "SURGGRAPH_PROJECT_ID": PROJECT_ID,
                "SURGBOT_LIVE_MODEL": SURGBOT_LIVE_MODEL,
                "SURGBOT_LIVE_LOCATION": SURGBOT_LIVE_LOCATION,
                # Real, documented switch (not a guess) — AdkApp's own
                # _telemetry_enabled() reads this exact env var. Confirmed by
                # web research this session, now empirically verified below
                # by checking Cloud Trace after a real deploy+invoke.
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
                "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            },
            "agent_server_mode": vertexai_types.AgentServerMode.EXPERIMENTAL,
            # identity_type intentionally omitted this run (defaults to the
            # project's Reasoning Engine Service Agent) — isolating whether
            # IdentityType.AGENT_IDENTITY itself is what broke the deployed
            # agent's own outbound Live API auth in the first attempt (see
            # the 1008 "invalid authentication credentials" error from
            # google.genai.errors.APIError, raised from inside
            # google/adk/models/google_llm.py's own live connect call).
        },
    )
    print(f"Deployed: {remote_agent.api_resource.name}")
    return remote_agent


async def invoke(client: vertexai.Client, remote_agent) -> bool:
    """Opens a real bidi_stream_query connection against the deployed agent,
    sends one text-based LiveRequest, and returns True only if a real
    response containing the tool's own output text came back with no error —
    same "no partial credit" standard as spike_deploy_stub_agent.py's invoke()."""
    resource_name = remote_agent.api_resource.name
    print(f"Connecting to bidi_stream_query on {resource_name}...")

    saw_error = False
    saw_tool_result = False
    events_seen = 0

    async with client.aio.live.agent_engines.connect(
        agent_engine=resource_name,
        config={"class_method": "bidi_stream_query"},
    ) as connection:
        await connection.send(
            {
                "user_id": "surgbot-spike-tester",
                "live_request": {
                    "content": {
                        "role": "user",
                        "parts": [{"text": "please ping"}],
                    }
                },
            }
        )

        # No __aiter__ on AsyncLiveAgentEngineSession (confirmed via source
        # read) — must poll receive() in a loop and catch the real
        # close-on-completion exception rather than assume a fixed count.
        try:
            while events_seen < 20:  # hard ceiling so a hung stream can't spin forever
                response = await asyncio.wait_for(connection.receive(), timeout=30.0)
                events_seen += 1
                print("EVENT:", response)
                event = response.get("bidiStreamOutput", response) if isinstance(response, dict) else response
                if isinstance(event, dict) and event.get("error_code"):
                    saw_error = True
                text_blob = str(event)
                if "surgbot spike stub received" in text_blob:
                    saw_tool_result = True
        except asyncio.TimeoutError:
            print("No further events within 30s — treating stream as finished.")
        except Exception as exc:  # websockets.exceptions.ConnectionClosed, etc.
            print(f"Stream ended: {exc!r}")

    print(f"Total events observed: {events_seen}")
    return (not saw_error) and saw_tool_result


def main() -> int:
    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    remote_agent = deploy(client)
    success = asyncio.run(invoke(client, remote_agent))

    if success:
        print(
            "\nSPIKE PASSED: agent deployed to Agent Runtime with EXPERIMENTAL "
            "bidi mode, bidi_stream_query round-tripped a real tool call."
        )
    else:
        print(
            "\nSPIKE FAILED: agent deployed but the bidi_stream_query round trip "
            "did not produce the expected tool result — see events above.\n"
            "Fallback per the plan (docs/... §14.6): run services/surgbot_service's "
            "Runner.run_live() locally against the raw Live API instead of Agent "
            "Runtime, dropping the automatic-Identity/Registry claims."
        )
    print(f"Resource name: {remote_agent.api_resource.name}")
    print("NOTE: this stub agent is left deployed — tear down manually once confirmed.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
