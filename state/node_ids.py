"""Node-ID builders — the single source of truth for the graph's ID conventions.

The store has no foreign-key enforcement: an edge referencing a node_id nobody
ever writes is accepted silently and simply never renders. Correctness depends
entirely on every writer agreeing on the same convention, which is exactly the
kind of agreement that erodes the moment two agents each format their own
f-string (docs/agentic_workflow.md §12: "Convention violations silently orphan
edges").

So no agent formats a node_id itself. Every ID in the system comes from a
function here, which makes a convention drift a change to one file rather than
a bug that shows up as a mysteriously missing edge three layers downstream.

Conventions are exactly as specified in docs/agentic_workflow.md §12; the
handful this module adds beyond that table (patient twin, vitals, action
outcome) follow the same shape and are marked below.
"""

from __future__ import annotations

import re

# --- Slugs -----------------------------------------------------------------
# Several conventions embed a free-text slug (complication names, corrective
# action names, Anticipation's own predicted phase wording). Those slugs are
# how a prediction later reconciles against a realized state, so the
# normalization has to be identical everywhere — hence one function, not one
# per caller.

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase, collapse any run of non-alphanumerics to a single underscore.

    Deliberately aggressive: "Bladder-neck injury" and "bladder neck injury"
    must produce the same slug, or the same real complication reasoned twice
    fragments into two graph nodes.
    """
    return _SLUG_STRIP.sub("_", text.strip().lower()).strip("_")


# --- Structural ------------------------------------------------------------


def trigger(case_id: str) -> str:
    """The case-open event every other node ultimately hangs off."""
    return f"trigger:{case_id}"


def agent(name: str) -> str:
    return f"agent:{name}"


def patient_twin() -> str:
    """Singleton per case — case scoping is already handled by the store's
    per-case partition, so no case_id in the ID itself."""
    return "patient_twin"


# --- Perception (docs/plan_v2 §7) ------------------------------------------


def entity(stable_id: str) -> str:
    """`stable_id` is a canonical string the perception step commits to
    (instrument_needle_driver_left), never a fresh UUID per window — the
    entity registry collapses into per-window duplicates otherwise."""
    return f"entity:{stable_id}"


def perception_event(seq: int, event_kind: str) -> str:
    return f"event:{seq}:{event_kind}"


def snapshot(slot: str) -> str:
    return f"snapshot:{slot}"


# The four fixed slots from plan_v2 §7.4. Their cardinality never grows with
# case length — that is the whole point of the snapshot tier.
SNAPSHOT_CURRENT_PHASE = snapshot("current_phase")
SNAPSHOT_CURRENT_ACTIVITY = snapshot("current_activity")
SNAPSHOT_ACTIVE_ENTITY_SET = snapshot("active_entity_set")
SNAPSHOT_CURRENT_VITALS = snapshot("current_vitals_summary")


def phase(opaque_id: str | int, window_index: int) -> str:
    """The opaque numeric phase id is internal bookkeeping only — it keys node
    identity so the same real segment doesn't fragment across windows. It is
    never shown to a model and never rendered; the node's *label* carries an
    agent's own semantic description."""
    return f"phase:{opaque_id}:{window_index}"


def predicted_phase(phase_name: str) -> str:
    """Keyed by the slug of the model's own predicted wording, since no numeric
    id exists for a phase that hasn't happened yet. Convergence works when a
    later real observation slugifies to the same key."""
    return f"predicted-phase:{slugify(phase_name)}"


def vitals(window_index: int) -> str:
    """Not in §12's table — follows the same shape."""
    return f"vitals:{window_index}"


def manual_event(uid: str) -> str:
    """Not in §12's table — split out of the old shared `event:` namespace so a
    human-typed note is never confused with a perception event or an alarm."""
    return f"manual_event:{uid}"


# --- Reasoning chain -------------------------------------------------------


def error(window: str | int, category: str) -> str:
    """`window` is the coordinator's real window_id (or a plain index). Keying
    by window+category rather than a per-event uuid means a re-detection of the
    same real error updates one node instead of creating a duplicate."""
    return f"error:{window}:{category}"


def complication(root_error_id: str, name: str) -> str:
    return f"complication:{root_error_id}:{slugify(name)}"


def literature_evidence(query_hash: str, result_index: int) -> str:
    return f"literature:{query_hash}:{result_index}"


def corrective_trajectory(root_error_id: str, name: str) -> str:
    return f"corrective:{root_error_id}:{slugify(name)}"


def divergence_alert(proposal_id: str, window_index: int) -> str:
    return f"divergence:{proposal_id}:{window_index}"


# --- Action + safety -------------------------------------------------------


def action_intent(kind: str, uid: str) -> str:
    return f"action_intent:{kind}:{uid}"


def verification_block(action_intent_id: str) -> str:
    return f"verification:{action_intent_id}"


def model_armor_screen(subject_id: str) -> str:
    """Same shape as verification_block — a second, independent fail-closed
    gate, not a competing convention. Keyed by whatever it's screening: the
    documentation node id at draft time (runs autonomously, before a surgeon
    ever sees an Approve button) and again at approval time (re-screening
    whatever was actually edited) — one node, refreshed twice, not two."""
    return f"model_armor:{subject_id}"


def action_outcome(action_intent_id: str) -> str:
    """Not in §12's table — mirrors verification_block's shape, keyed by the
    intent it reports on so the outcome edge can never dangle."""
    return f"action_outcome:{action_intent_id}"


# --- Post-case -------------------------------------------------------------


def benchmark(case_id: str) -> str:
    return f"benchmark:{case_id}"


def documentation(case_id: str) -> str:
    return f"documentation:{case_id}"


# --- Edges -----------------------------------------------------------------


def edge(source_node_id: str, target_node_id: str, edge_kind: str) -> str:
    """Deterministic from its endpoints and kind, so re-writing the same
    logical edge updates it in place instead of duplicating it. Two different
    kinds of edge between the same pair stay distinct."""
    return f"{edge_kind}|{source_node_id}->{target_node_id}"


# --- Validation ------------------------------------------------------------

_PREFIX_TO_NODE_TYPE = {
    "trigger": "trigger",
    "agent": "agent",
    "patient_twin": "patient_twin",
    "entity": "entity",
    "event": "perception_event",
    "snapshot": "snapshot",
    "phase": "phase",
    "predicted-phase": "phase",
    "vitals": "vitals",
    "manual_event": "manual_event",
    "error": "error",
    "complication": "complication",
    "literature": "literature_evidence",
    "corrective": "corrective_trajectory",
    "divergence": "divergence_alert",
    "action_intent": "action_intent",
    "verification": "verification_block",
    "model_armor": "model_armor_screen",
    "action_outcome": "action_outcome",
    "benchmark": "benchmark",
    "documentation": "documentation",
}


def node_type_for(node_id: str) -> str | None:
    """Returns the node_type a given id implies, or None if the id follows no
    known convention. Used by the orphaned-edge reconciliation check to flag
    convention violations onto the case's own graph as visible warnings,
    rather than letting them fail silently."""
    prefix = node_id.split(":", 1)[0]
    return _PREFIX_TO_NODE_TYPE.get(prefix)
