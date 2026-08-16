"""Loader and validator for the bounded corrective-action library.

The library is data, not code (data/corrective_actions/library.json), so it can
be reviewed as clinical content by someone who does not read Python. This module
is the only thing that reads it, and it is also where the "selects, never
generates" constraint is actually ENFORCED — the agent's prompt asks for
action_ids, but a prompt is a request, not a guarantee. `resolve` drops any id
that is not really in the library for that category, so a hallucinated action
cannot reach the graph even if the model produces one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LIBRARY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "corrective_actions" / "library.json"

_cache: dict | None = None


@dataclass(frozen=True)
class CorrectiveAction:
    action_id: str
    action: str
    rationale: str
    verification_check: str
    inverts_indicator: str


def _load() -> dict:
    global _cache
    if _cache is None:
        # No fallback: a missing or malformed library must fail loudly. Silently
        # proceeding with an empty action set would make every proposal an
        # escalation and look like the model declining, not a broken file.
        with open(LIBRARY_PATH) as f:
            _cache = json.load(f)
    return _cache


def provenance() -> dict:
    """Carried onto every proposal node. These are surgical suggestions and the
    strength of their sourcing travels with them, not just with the file."""
    return _load()["_provenance"]


def actions_for(category: str) -> list[CorrectiveAction]:
    entries = _load()["categories"].get(category, [])
    return [CorrectiveAction(**e) for e in entries]


def format_for_prompt(category: str) -> str:
    """The library rendered as the numbered menu the agent selects from."""
    actions = actions_for(category)
    if not actions:
        return "(no corrective actions are defined for this error category — escalate)"
    lines = []
    for a in actions:
        lines.append(f"  action_id: {a.action_id}")
        lines.append(f"    action: {a.action}")
        lines.append(f"    rationale: {a.rationale}")
        lines.append(f"    verification check: {a.verification_check}")
    return "\n".join(lines)


def resolve(category: str, action_ids: list[str]) -> tuple[list[CorrectiveAction], list[str]]:
    """Maps selected ids back to real library entries.

    Returns (resolved, rejected). Anything not genuinely in the library for this
    category is rejected and never reaches the graph — this is the enforcement
    behind "selects, never generates", since the prompt alone cannot guarantee
    the model stays inside the vocabulary.
    """
    by_id = {a.action_id: a for a in actions_for(category)}
    resolved, rejected = [], []
    for action_id in action_ids:
        action = by_id.get(action_id)
        if action is None:
            rejected.append(action_id)
        else:
            resolved.append(action)
    if rejected:
        logger.warning(
            "corrective_replanning: rejected %d action id(s) not in the %s library: %s",
            len(rejected),
            category,
            rejected,
        )
    return resolved, rejected
