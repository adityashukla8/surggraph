"""Day 1 Spike B: prove an ADK agent can actually be deployed to GEAP's
Agent Runtime and invoked, before 13 more agents get built on top of the
assumption that this works. Throwaway stub — the real Orchestrator lives in
agents/orchestrator/agent.py and is built later in the schedule.

Found during this spike: gemini-3.5-flash (the hackathon's mandatory model)
returns 404 NOT_FOUND on every regional Vertex AI endpoint for this project
(us-central1, us-east5, us-east1, europe-west4 all failed; gemini-2.5-flash
works regionally, but that's older, not "3.5 or newer" as required) — it is
only reachable via the `global` Vertex AI location. ADK's Gemini model
wrapper doesn't expose `location` as a constructor field, so this uses the
documented subclass-override pattern (see google.adk.models.Gemini docstring)
to force the global endpoint.

Usage: uv run scripts/spike_deploy_stub_agent.py
"""

from __future__ import annotations

import asyncio
import os

import vertexai
from dotenv import load_dotenv
from google.adk.agents import Agent
from vertexai import agent_engines

from tools.gemini_model import GEMINI_MODEL, new_agent_model

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")  # Agent Runtime hosting region
STAGING_BUCKET = f"gs://{os.environ['SURGGRAPH_GCS_BUCKET']}"


def ping(message: str = "pong") -> str:
    """A single dummy tool — just proves the deployed agent can receive a
    tool-calling request and return a result."""
    return f"stub agent received: {message}"


def deploy(client: vertexai.Client):
    stub_agent = Agent(
        model=new_agent_model(),
        name="surggraph_spike_stub_agent",
        instruction="You are a smoke-test stub. When asked to ping, call the ping tool and report its result.",
        tools=[ping],
    )
    app = agent_engines.AdkApp(agent=stub_agent)

    print(f"Deploying stub agent to Agent Runtime (project={PROJECT_ID}, region={REGION}, model={GEMINI_MODEL}@global)...")
    remote_agent = client.agent_engines.create(
        agent=app,
        config={
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]",
                "pydantic",
                "cloudpickle",
                "python-dotenv",
            ],
            "extra_packages": ["tools"],
            "staging_bucket": STAGING_BUCKET,
            "env_vars": {
                "SURGGRAPH_PROJECT_ID": PROJECT_ID,
                "GEMINI_MODEL": GEMINI_MODEL,
                "GEMINI_LOCATION": os.environ.get("GEMINI_LOCATION", "global"),
            },
        },
    )
    print(f"Deployed: {remote_agent.api_resource.name}")
    return remote_agent


async def invoke(remote_agent) -> bool:
    """Returns True only if the agent actually produced a real text response
    with no error_code in any event — do not report success on a partial or
    error-carrying response."""
    print("Creating session...")
    session = await remote_agent.async_create_session(user_id="spike-tester")
    session_id = session.get("id")
    print("Session created:", session_id)

    print("Invoking deployed agent...")
    saw_error = False
    saw_tool_result = False
    async for event in remote_agent.async_stream_query(
        user_id="spike-tester",
        session_id=session_id,
        message="please ping",
    ):
        print(event)
        if isinstance(event, dict) and event.get("error_code"):
            saw_error = True
        content = event.get("content") if isinstance(event, dict) else None
        if content and "stub agent received" in str(content):
            saw_tool_result = True

    return (not saw_error) and saw_tool_result


def main() -> int:
    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    remote_agent = deploy(client)
    success = asyncio.run(invoke(remote_agent))

    if success:
        print("\nSPIKE B PASSED: agent deployed and invoked on Agent Runtime, tool call round-tripped correctly.")
    else:
        print("\nSPIKE B FAILED: agent deployed but invocation did not produce the expected tool result — see events above.")
    print(f"Resource name: {remote_agent.api_resource.name}")
    print("NOTE: this stub agent is left deployed — tear down manually once confirmed, or leave as the Day-1 proof point.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
