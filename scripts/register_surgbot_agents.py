"""Registers SurgBot's root agent and its synthesis subagent in GEAP Agent
Registry — for real, via `gcloud agent-registry services create`, not just
code that "should" work.

Confirmed this session (docs.cloud.google.com/gemini-enterprise-agent-
platform/govern/agent-registry "Register agents"): Agent Runtime deployment
does NOT auto-register an agent in Agent Registry (verified empirically —
`gcloud agent-registry agents list` and `gcloud alpha agent-registry
services list` against the live spike deployment showed nothing but
Google's own default "Workspace Agent"). There IS a real, STABLE (non-alpha)
command for this, found by reading `gcloud agent-registry --help` directly
rather than assuming REST was the only option: `gcloud agent-registry
services create` — "Create a writable Service resource to manually register
custom or external agentic components into the registry." No `agents
create` subcommand exists (`agents` only supports describe/list/search — a
Service is what auto-projects onto the consumer-facing read-only Agent view),
confirmed via `gcloud agent-registry agents --help`.

Registers with --agent-spec-type=no-spec (no A2A Agent Card is served by
either deployment) and one --interfaces entry pointing at the real Vertex AI
REST endpoint for querying that Reasoning Engine
(https://{region}-aiplatform.googleapis.com/v1/{resource_name}:streamQuery) —
the genuine, documented REST surface for invoking a deployed Reasoning
Engine, not a placeholder URL.

Reads the deployed resource names from the two local deploy-cache files
scripts/deploy_surgbot_agent.py and agents/surgbot/subagents.py already
write — run those deploy scripts first.

Usage: uv run python3 scripts/register_surgbot_agents.py
Then verify: gcloud agent-registry services list --location=$SURGGRAPH_REGION
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")

_ROOT_AGENT_CACHE = Path(__file__).resolve().parent / ".deployed_root_agent.json"
_SUBAGENT_CACHE = Path(__file__).resolve().parent.parent / "agents" / "surgbot" / ".deployed_subagents.json"


def _read_cache(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"deploy cache not found: {path} — run the matching deploy script first")
    return json.loads(path.read_text())


def _rest_url(resource_name: str) -> str:
    return f"https://{REGION}-aiplatform.googleapis.com/v1/{resource_name}:streamQuery"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return result


def register_service(service_id: str, display_name: str, description: str, resource_name: str) -> bool:
    url = _rest_url(resource_name)
    cmd = [
        "gcloud", "agent-registry", "services", "create", service_id,
        f"--location={REGION}",
        f"--project={PROJECT_ID}",
        f"--display-name={display_name}",
        f"--description={description}",
        "--agent-spec-type=no-spec",
        f"--interfaces=url={url},protocolBinding=http-json",
    ]
    result = _run(cmd)
    if result.returncode == 0:
        return True
    # ALREADY_EXISTS means a PRIOR deployment already registered this
    # service_id — real bug caught in this session's own verification pass:
    # redeploying root_agent/synthesis mints a NEW reasoning-engine resource
    # name, but `create` on an existing service_id is a no-op, silently
    # leaving the registry pointed at a stale, superseded engine. Fall
    # through to `update` so a re-run of this script actually repoints the
    # registered interface at the current resource_name, not just report
    # success on a URL that no longer matches what's deployed.
    if "already exists" in (result.stderr or "").lower() or "ALREADY_EXISTS" in (result.stderr or ""):
        print(f"{service_id} already registered — updating its interface to the current resource_name.")
        update_cmd = [
            "gcloud", "agent-registry", "services", "update", service_id,
            f"--location={REGION}",
            f"--project={PROJECT_ID}",
            f"--interfaces=url={url},protocolBinding=http-json",
        ]
        update_result = _run(update_cmd)
        return update_result.returncode == 0
    return False


def main() -> int:
    root_cache = _read_cache(_ROOT_AGENT_CACHE)
    root_resource = root_cache.get("resource_name")
    if not root_resource:
        raise SystemExit(f"{_ROOT_AGENT_CACHE} has no resource_name — run scripts/deploy_surgbot_agent.py first")

    subagent_cache = _read_cache(_SUBAGENT_CACHE)
    synthesis_resource = subagent_cache.get("synthesis")
    if not synthesis_resource:
        raise SystemExit(f"{_SUBAGENT_CACHE} has no 'synthesis' entry — run scripts/deploy_surgbot_subagents.py first")

    print(f"Root agent resource: {root_resource}")
    print(f"Synthesis subagent resource: {synthesis_resource}")

    ok_root = register_service(
        "surgbot-root-agent",
        "SurgBot Root Agent",
        "SurgBot's Live-API voice/turn-taking root agent — conversational cross-case surgical case review (Track 2), dispatching real reasoning to Gemini 3.5 Agent Runtime subagents.",
        root_resource,
    )
    ok_synthesis = register_service(
        "surgbot-synthesis-subagent",
        "SurgBot Synthesis Subagent",
        "SurgBot's Phase 6 case-review synthesis subagent — real Gemini 3.5, deployed with Agent Identity.",
        synthesis_resource,
    )

    print("\n=== Verifying via `gcloud agent-registry services list` ===")
    verify = _run(["gcloud", "agent-registry", "services", "list", f"--location={REGION}", f"--project={PROJECT_ID}"])

    success = ok_root and ok_synthesis and verify.returncode == 0
    if success:
        print("\nREGISTRATION SUCCEEDED for both agents — see the services list above.")
    else:
        print("\nREGISTRATION DID NOT FULLY SUCCEED — see output above.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
