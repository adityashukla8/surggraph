"""Deploys (or reuses) all three SurgBot TEXT reasoning subagents —
error_chain_reviewer, synthesis, pattern_insight — to GEAP Agent Runtime,
each as its OWN separate reasoning engine with identity_type=AGENT_IDENTITY,
then smoke-tests each with a trivial real prompt and prints the real
response.

Real, empirically-confirmed architecture decision this session: unlike
SurgBot's root Live agent (whose own outbound Live API call breaks under
AGENT_IDENTITY — see agents/surgbot/live_model.py), a plain Gemini-3.5
tool-calling agent deployed with identity_type=AGENT_IDENTITY worked
end-to-end in a real deploy+invoke spike. So every SurgBot subagent gets real
Agent Identity — "everything GEAP deployed" is a literal requirement, not
just a Runtime-for-the-root-agent claim.

Deployment + caching logic lives in agents/surgbot/subagents.py::
deploy_or_get_subagent — this script just calls it for each kind and reports
results. Safe to re-run: an already-deployed subagent (found via the local
cache file or a live client.agent_engines.list() scan by display_name) is
reused, not redeployed.

Usage: uv run python3 scripts/deploy_surgbot_subagents.py
"""

from __future__ import annotations

import asyncio
import os

import vertexai
from dotenv import load_dotenv

from agents.surgbot.subagents import SUBAGENT_KINDS, deploy_or_get_subagent, invoke_subagent

load_dotenv()

PROJECT_ID = os.environ["SURGGRAPH_PROJECT_ID"]
REGION = os.environ.get("SURGGRAPH_REGION", "us-central1")

_SMOKE_MESSAGES = {
    "error_chain_reviewer": (
        '{"found": true, "error": {"id": "error-smoke-1", "type": "error", '
        '"label": "smoke test error", "attrs": {"error_category": "manual_injection"}}, '
        '"complications": [], "literature_by_complication": {}, "corrective_proposals_by_complication": {}}'
    ),
    "synthesis": (
        '{"case_ids": ["case-smoke-1"], "reviewer_id": "smoke-tester@example.com", '
        '"case_framings": {"case-smoke-1": {"case_id": "case-smoke-1", "phase_count": 0, '
        '"error_count": 0, "complication_count": 0, "active_proposal_count": 0}}, '
        '"feedback_items": []}'
    ),
    "pattern_insight": '{"reviewer_id": "smoke-tester@example.com", "memories": []}',
}


def deploy_all() -> dict[str, str]:
    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    resource_names: dict[str, str] = {}
    for kind in SUBAGENT_KINDS:
        print(f"\n=== Deploying/resolving subagent: {kind} ===")
        engine = deploy_or_get_subagent(kind, client)
        resource_name = engine.api_resource.name
        resource_names[kind] = resource_name
        print(f"{kind} -> {resource_name}")
    return resource_names


async def smoke_test_all(client: vertexai.Client) -> bool:
    all_ok = True
    for kind in SUBAGENT_KINDS:
        print(f"\n=== Smoke test: {kind} ===")
        message = _SMOKE_MESSAGES[kind]
        print(f"INPUT message: {message}")
        result = await invoke_subagent(kind, message, user_id="surgbot-deploy-smoke-test", client=client)
        print(f"RAW result: {result}")
        ok = result.get("parsed") is not None
        all_ok = all_ok and ok
        print(f"{kind}: {'PASSED' if ok else 'FAILED'} (parsed={'yes' if ok else 'no'}, error={result.get('error')})")
    return all_ok


def main() -> int:
    print(f"Deploying SurgBot subagents (project={PROJECT_ID}, region={REGION})...")
    resource_names = deploy_all()

    client = vertexai.Client(project=PROJECT_ID, location=REGION)
    success = asyncio.run(smoke_test_all(client))

    print("\n=== Summary ===")
    for kind, name in resource_names.items():
        print(f"  {kind}: {name}")
    if success:
        print("\nALL SUBAGENTS DEPLOYED AND SMOKE-TESTED SUCCESSFULLY.")
    else:
        print("\nAT LEAST ONE SUBAGENT SMOKE TEST FAILED — see output above.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
