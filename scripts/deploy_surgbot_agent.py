"""Deploys SurgBot's root agent to GEAP Agent Runtime in EXPERIMENTAL
bidi-streaming mode, then smoke-tests it via a real bidi_stream_query
round trip with a text turn (no synthesized PCM audio needed — Live API
supports text-based turns as a documented interaction mode even in an
audio-primary session, confirmed empirically below).

Extends scripts/spike_surgbot_live_roundtrip.py's already-proven deploy
shape: vertexai.preview.reasoning_engines.AdkApp (NOT vertexai.agent_engines
— the STABLE class has no bidi support), agent_server_mode=EXPERIMENTAL, NO
identity_type (AGENT_IDENTITY breaks this agent's own outbound Live API call
— see agents/surgbot/live_model.py). This is now the load-bearing deploy
path for the real root_agent.py (8 real tools, several of which dispatch to
the separately-deployed SurgBot subagents), not a throwaway stub.

The deployed resource's name is cached in .deployed_root_agent.json next to
this script (gitignored) so re-running this script reuses an existing
deployment instead of redeploying every time root_agent.py's tools haven't
actually changed — pass --force to redeploy unconditionally (needed whenever
root_agent.py's tool set changes, per plan §14.6 Day 4's note that every
tool-set change needs a fresh deploy).

Usage: uv run python3 scripts/deploy_surgbot_agent.py [--force]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import vertexai
from dotenv import load_dotenv
from vertexai import types as vertexai_types
from vertexai.preview.reasoning_engines import AdkApp

from agents.surgbot.live_model import SURGBOT_LIVE_LOCATION, SURGBOT_LIVE_MODEL, SURGBOT_LIVE_VOICE
from agents.surgbot.root_agent import build_root_agent
from agents.surgbot.subagents import SUBAGENT_KINDS
from agents.surgbot.subagents import _DEPLOY_CACHE_PATH as _SUBAGENT_CACHE_PATH

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")
STAGING_BUCKET = f"gs://{os.environ['SURGGRAPH_GCS_BUCKET']}"

DISPLAY_NAME = "surgbot-root-agent"
_CACHE_PATH = Path(__file__).resolve().parent / ".deployed_root_agent.json"

# Real bug found via Cloud Logging this session (aiplatform.googleapis.com/
# reasoning_engine_stderr on this exact deployment, right after a genuine
# `list_accessible_cases` function_call round-tripped correctly): with no
# identity_type set, root_agent runs as the project's default Reasoning
# Engine Service Agent (service-{PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.
# gserviceaccount.com, confirmed via `gcloud projects get-iam-policy`), which
# has NO Firestore role — case_index.py's Firestore call inside the deployed
# sandbox failed with a real PERMISSION_DENIED, which surfaced to the client
# as a generic "Reasoning Engine Execution failed" / resource_exhausted
# close. Fix: run root_agent AS the SAME existing runtime service account
# services/state_service and services/orchestrator_service already use for
# Cloud Run (confirmed via IAM policy to already hold roles/datastore.user
# AND roles/modelarmor.user — exactly the two things this agent's tools need
# and the default Reasoning Engine Service Agent lacks), via the
# `service_account` config field — mutually exclusive with identity_type
# (IdentityType.AGENT_IDENTITY's own docstring: "the service_account field
# must not be set"), which is fine since root_agent already omits
# identity_type for the separate Live-API-auth reason above.
SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT = os.environ.get(
    "SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT", f"surggraph-runtime@{PROJECT_ID}.iam.gserviceaccount.com"
)

_TELEMETRY_ENV_VARS = {
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
}


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _subagent_resource_env_vars() -> dict[str, str]:
    """Real bug found via Cloud Logging this session: the deployed root
    agent's tools (subagents.invoke_subagent, retrieve_reviewer_patterns)
    call subagents.deploy_or_get_subagent(), which was falling back to a
    live client.agent_engines.list() scan on EVERY call — because the
    deployed sandbox is a different filesystem than this machine and never
    has agents/surgbot/.deployed_subagents.json. Bake each already-deployed
    subagent's real resource name in here instead (same STATE_SERVICE_URL
    pattern already used below), so the common path is one cheap
    client.agent_engines.get() by exact name. Missing entries are simply
    omitted — deploy_or_get_subagent()'s cache-file/list-scan fallback still
    covers a subagent that hasn't been deployed yet, so this is a real
    latency optimization, not a hard requirement to deploy root_agent.
    """
    if not _SUBAGENT_CACHE_PATH.exists():
        print(f"Note: {_SUBAGENT_CACHE_PATH} not found — root agent will discover subagents via live list() scan.")
        return {}
    try:
        cache = json.loads(_SUBAGENT_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Note: could not read {_SUBAGENT_CACHE_PATH} ({exc}) — falling back to live list() scan.")
        return {}
    env_vars = {}
    for kind in SUBAGENT_KINDS:
        resource_name = cache.get(kind)
        if resource_name:
            env_vars[f"SURGBOT_SUBAGENT_RESOURCE_{kind.upper()}"] = resource_name
    return env_vars


def _find_existing(client: vertexai.Client):
    for engine in client.agent_engines.list():
        if engine.api_resource.display_name == DISPLAY_NAME:
            return engine
    return None


def deploy(client: vertexai.Client, force: bool = False):
    cache = _load_cache()

    if not force:
        cached_name = cache.get("resource_name")
        if cached_name:
            try:
                existing = client.agent_engines.get(name=cached_name)
                print(f"Reusing cached root agent deployment: {cached_name}")
                return existing
            except Exception:
                print(f"Cached resource {cached_name} no longer resolves, re-checking live list...")
        found = _find_existing(client)
        if found is not None:
            cache["resource_name"] = found.api_resource.name
            _save_cache(cache)
            print(f"Found existing live deployment: {found.api_resource.name}")
            return found

    root_agent = build_root_agent()
    app = AdkApp(agent=root_agent)  # preview class — has real bidi_stream_query

    print(
        f"Deploying SurgBot root agent to Agent Runtime "
        f"(project={PROJECT_ID}, region={REGION}, live_model={SURGBOT_LIVE_MODEL}@{SURGBOT_LIVE_LOCATION}, "
        f"voice={SURGBOT_LIVE_VOICE}, agent_server_mode=EXPERIMENTAL, 8 real tools)..."
    )
    engine = client.agent_engines.create(
        agent=app,
        config={
            "display_name": DISPLAY_NAME,
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]",
                "pydantic",
                "cloudpickle",
                "python-dotenv",
                "google-cloud-firestore",
                "google-cloud-modelarmor",
                "httpx",
                "requests",
            ],
            "extra_packages": ["agents", "tools", "state"],
            "staging_bucket": STAGING_BUCKET,
            "env_vars": {
                "SURGGRAPH_PROJECT_ID": PROJECT_ID,
                "SURGGRAPH_REGION": REGION,
                "SURGGRAPH_GCS_BUCKET": os.environ["SURGGRAPH_GCS_BUCKET"],
                "SURGBOT_LIVE_MODEL": SURGBOT_LIVE_MODEL,
                "SURGBOT_LIVE_LOCATION": SURGBOT_LIVE_LOCATION,
                # Real gap found this session: speech_config is set on the
                # model itself (live_model.py), which is built INSIDE the
                # deployed sandbox — its own env, not this local process's —
                # so voice selection must be baked in at deploy time too, the
                # same way SURGBOT_LIVE_MODEL/LOCATION already are.
                "SURGBOT_LIVE_VOICE": SURGBOT_LIVE_VOICE,
                "GEMINI_MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
                "GEMINI_LOCATION": os.environ.get("GEMINI_LOCATION", "global"),
                "FIRESTORE_DATABASE": os.environ.get("FIRESTORE_DATABASE", "(default)"),
                # Real bug found this session (Cloud Logging: httpx.
                # RemoteProtocolError "illegal request line" on every
                # load_case_graph call): this used to read STATE_SERVICE_URL
                # directly, silently baking THIS MACHINE's own local-dev
                # loopback address (http://127.0.0.1:8080, meaningless inside
                # the deployed sandbox) into the agent's env. A separate,
                # deploy-only var — required, not silently defaulted to ""
                # — so this can never again fail silently.
                "STATE_SERVICE_URL": os.environ["SURGBOT_DEPLOYED_STATE_SERVICE_URL"],
                **_subagent_resource_env_vars(),
                **_TELEMETRY_ENV_VARS,
            },
            "agent_server_mode": vertexai_types.AgentServerMode.EXPERIMENTAL,
            "service_account": SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT,
            # NO identity_type — see agents/surgbot/live_model.py's docstring
            # and this file's module docstring: AGENT_IDENTITY breaks this
            # agent's own outbound Live API call, confirmed twice this
            # session. (identity_type and service_account are mutually
            # exclusive anyway — see this file's module-level comment above.)
        },
    )
    cache["resource_name"] = engine.api_resource.name
    _save_cache(cache)
    print(f"Deployed: {engine.api_resource.name}")
    return engine


async def smoke_test(client: vertexai.Client, engine) -> bool:
    """Opens a real bidi_stream_query connection and sends ONE text turn
    (Live API documents text-based turns as a valid interaction mode even in
    an audio-primary session — this avoids needing synthesized PCM audio just
    to prove the deployed agent's tool-calling path actually works)."""
    resource_name = engine.api_resource.name
    print(f"\nConnecting to bidi_stream_query on {resource_name}...")

    saw_error = False
    saw_tool_call = False
    saw_text = False
    events_seen = 0

    async with client.aio.live.agent_engines.connect(
        agent_engine=resource_name,
        config={"class_method": "bidi_stream_query"},
    ) as connection:
        await connection.send(
            {
                "user_id": "surgbot-deploy-smoke-test",
                "live_request": {
                    "content": {
                        "role": "user",
                        "parts": [{"text": "Please list the accessible cases to start a review session."}],
                    }
                },
            }
        )

        try:
            # 40 was too small: the Live model speaks its mandatory
            # disclosure sentence first (confirmed real this session — a
            # correct, full disclosure of the Live-vs-Gemini-3.5 split), and
            # that alone chops into dozens of small audio+transcript events
            # before the model ever gets to calling a tool. Real observed
            # count for "disclosure sentence, no tool call yet" was 40/40
            # exhausted with zero tool calls — raised well past that.
            while events_seen < 200:
                response = await asyncio.wait_for(connection.receive(), timeout=45.0)
                events_seen += 1
                print("EVENT:", json.dumps(response)[:800])
                event = response.get("bidiStreamOutput", response) if isinstance(response, dict) else response
                if isinstance(event, dict):
                    if event.get("error_code"):
                        saw_error = True
                    content = event.get("content") or {}
                    for part in content.get("parts", []) or []:
                        if part.get("function_call") or part.get("function_response"):
                            saw_tool_call = True
                        if part.get("text"):
                            saw_text = True
        except asyncio.TimeoutError:
            print("No further events within 45s — treating stream as finished.")
        except Exception as exc:
            print(f"Stream ended: {exc!r}")

    print(f"\nTotal events observed: {events_seen}")
    print(f"saw_tool_call={saw_tool_call} saw_text={saw_text} saw_error={saw_error}")
    return (not saw_error) and saw_tool_call


def main() -> int:
    force = "--force" in sys.argv
    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    engine = deploy(client, force=force)
    success = asyncio.run(smoke_test(client, engine))

    if success:
        print("\nROOT AGENT DEPLOY + SMOKE TEST PASSED: real tool call round-tripped over bidi_stream_query.")
    else:
        print("\nROOT AGENT SMOKE TEST DID NOT CONFIRM A TOOL CALL — see events above.")
    print(f"Resource name: {engine.api_resource.name}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
