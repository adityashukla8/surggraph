"""Deploys SurgBot's root agent to GEAP Agent Runtime as a STABLE
vertexai.agent_engines.AdkApp, then smoke-tests it via a real
async_stream_query call — the exact deploy+invoke shape
scripts/deploy_surgbot_subagents.py already uses successfully for three
real subagents (plan_v2 §15 migrated root_agent.py onto this SAME path,
off the EXPERIMENTAL bidi_stream_query transport it used to require for
Live API audio — real, repeated crashes there are the reason: a proprietary
internal queue overflow on barge-in, and a real ~10-minute bidi_stream_query
session ceiling hit live with a failed ADK auto-reconnect).

The deployed resource's name is cached in .deployed_root_agent.json next to
this script (gitignored) so re-running this script reuses an existing
deployment instead of redeploying every time root_agent.py's tools haven't
actually changed — pass --force to redeploy unconditionally (needed whenever
root_agent.py's tool set changes).

Usage: uv run python3 scripts/deploy_surgbot_agent.py [--force] [--agent-identity]

--agent-identity swaps the deploy's service_account for
identity_type=AGENT_IDENTITY (real SPIFFE-based Agent Identity, ASI03-hardening
per docs.cloud.google.com/agent-builder/agent-engine/agent-identity) as a
controlled, reversible experiment — see the module-level comment above
SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT for why this wasn't the default and what to
check if it's used.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import vertexai
from dotenv import load_dotenv
from vertexai import agent_engines
from vertexai import types as vertexai_types

from agents.surgbot.root_agent import build_root_agent
from agents.surgbot.speech import SPEECH_LANGUAGE_CODE, SPEECH_REGION, TTS_REGION, TTS_VOICE
from agents.surgbot.subagents import SUBAGENT_KINDS
from agents.surgbot.subagents import _DEPLOY_CACHE_PATH as _SUBAGENT_CACHE_PATH
from tools.gemini_model import GEMINI_MODEL

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")
STAGING_BUCKET = f"gs://{os.environ['SURGGRAPH_GCS_BUCKET']}"

DISPLAY_NAME = "surgbot-root-agent"
_CACHE_PATH = Path(__file__).resolve().parent / ".deployed_root_agent.json"

# Real bug found via Cloud Logging earlier this session: with no
# identity_type/service_account set, root_agent runs as the project's
# default Reasoning Engine Service Agent, which has NO Firestore role.
# Run root_agent AS the SAME existing runtime service account
# services/state_service and services/orchestrator_service already use
# (confirmed via IAM policy to hold roles/datastore.user AND
# roles/modelarmor.user — exactly what this agent's tools need).
#
# --agent-identity follow-up (2026-08-28): the standard GEAP fix for a
# shared, over-broad service account is a per-agent Agent Identity
# (SPIFFE + auto-provisioned X.509 cert, no long-lived key). Its IAM-grant
# principal format is
#   principal://agents.global.org-ORG_ID.system.id.goog/resources/aiplatform/...
# which is hard-coded to require an org ID — `gcloud organizations list`
# returns 0 items for this project (no parent GCP organization exists), so
# that principal string cannot be constructed here at all, and no
# documented per-project-only fallback exists. This means an
# AGENT_IDENTITY-deployed root agent gets a real, auto-provisioned identity
# but CANNOT be granted roles/datastore.user or roles/modelarmor.user the
# normal way — expected failure mode is every Firestore/Model Armor tool
# call breaking with a permission error, same shape as the bug this
# comment block opens with. --agent-identity exists to test that
# prediction empirically rather than leave it as an assumption.
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


def deploy(client: vertexai.Client, force: bool = False, agent_identity: bool = False):
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
    app = agent_engines.AdkApp(agent=root_agent)  # STABLE class — no bidi needed anymore (plan_v2 §15)

    print(
        f"Deploying SurgBot root agent to Agent Runtime "
        f"(project={PROJECT_ID}, region={REGION}, model={GEMINI_MODEL}@global, "
        f"stt_region={SPEECH_REGION}, tts_region={TTS_REGION}, tts_voice={TTS_VOICE}, 9 real tools, "
        f"identity={'AGENT_IDENTITY (experimental test)' if agent_identity else SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT})..."
    )
    identity_config = (
        {"identity_type": vertexai_types.IdentityType.AGENT_IDENTITY}
        if agent_identity
        else {"service_account": SURGBOT_ROOT_AGENT_SERVICE_ACCOUNT}
    )
    engine = client.agent_engines.create(
        agent=app,
        config={
            "display_name": DISPLAY_NAME,
            "requirements": [
                "google-cloud-aiplatform[agent_engines,adk]",
                # Real bug found 2026-08-28: neither google-adk nor
                # google-genai is version-pinned anywhere in this project
                # (pyproject.toml doesn't pin them either) — a redeploy
                # months apart from the last one silently picked up a newer
                # google-adk release whose Gemini model class expects an
                # api_version attribute GlobalGemini's api_client override
                # (tools/gemini_model.py) doesn't provide, breaking every
                # single tool call with a real AttributeError. Pinned to
                # this session's confirmed-working local versions so a
                # future redeploy can't silently drift onto a breaking
                # release the same way.
                "google-adk==2.6.3",
                "google-genai==2.17.0",
                "pydantic",
                "cloudpickle",
                "python-dotenv",
                "google-cloud-firestore",
                "google-cloud-modelarmor",
                "google-cloud-speech",
                "google-cloud-texttospeech",
                "httpx",
                "requests",
            ],
            "extra_packages": ["agents", "tools", "state"],
            "staging_bucket": STAGING_BUCKET,
            "env_vars": {
                "SURGGRAPH_PROJECT_ID": PROJECT_ID,
                "SURGGRAPH_REGION": REGION,
                "SURGGRAPH_GCS_BUCKET": os.environ["SURGGRAPH_GCS_BUCKET"],
                "GEMINI_MODEL": GEMINI_MODEL,
                "GEMINI_LOCATION": os.environ.get("GEMINI_LOCATION", "global"),
                "SURGBOT_SPEECH_REGION": SPEECH_REGION,
                "SURGBOT_TTS_REGION": TTS_REGION,
                "SURGBOT_SPEECH_LANGUAGE_CODE": SPEECH_LANGUAGE_CODE,
                "SURGBOT_TTS_VOICE": TTS_VOICE,
                "FIRESTORE_DATABASE": os.environ.get("FIRESTORE_DATABASE", "(default)"),
                # Real bug found earlier this session (Cloud Logging: httpx.
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
            **identity_config,
        },
    )
    cache["resource_name"] = engine.api_resource.name
    _save_cache(cache)
    print(f"Deployed: {engine.api_resource.name}")
    return engine


async def smoke_test(engine) -> bool:
    """Real async_stream_query call — the exact pattern
    scripts/deploy_surgbot_subagents.py::smoke_test_all already proves works
    for this deployment class. No Live API/bidi/audio involved at all."""
    print("\nSending a real text turn via async_stream_query...")

    saw_tool_call = False
    saw_text = False
    final_text = ""

    async for event in engine.async_stream_query(
        user_id="surgbot-deploy-smoke-test",
        message="Please list the accessible cases to start a review session.",
    ):
        print("EVENT:", json.dumps(event)[:400])
        content = event.get("content") or {}
        for part in content.get("parts", []) or []:
            if part.get("function_call") or part.get("function_response"):
                saw_tool_call = True
            text = part.get("text")
            if text:
                saw_text = True
                final_text = text

    print(f"\nsaw_tool_call={saw_tool_call} saw_text={saw_text}")
    print(f"final_text={final_text!r}")
    return saw_tool_call and saw_text


def main() -> int:
    force = "--force" in sys.argv
    agent_identity = "--agent-identity" in sys.argv
    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    engine = deploy(client, force=force, agent_identity=agent_identity)
    success = asyncio.run(smoke_test(engine))

    if success:
        print("\nROOT AGENT DEPLOY + SMOKE TEST PASSED: real tool call round-tripped over async_stream_query.")
    else:
        print("\nROOT AGENT SMOKE TEST DID NOT CONFIRM A TOOL CALL — see events above.")
    print(f"Resource name: {engine.api_resource.name}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
