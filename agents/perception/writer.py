"""Turns a PerceptionPipeline decision into real graph writes.

docs/plan_v2_autonomous_safety_system.md §7.2-§7.4, §7.9. Three destinations,
deliberately kept apart:

  ENTITY REGISTRY   long-lived nodes, updated IN PLACE. One node per distinct
                    real-world object for the whole case. An instrument that
                    leaves the field is marked inactive, never deleted, so its
                    identity survives occlusion.
  EVENT STREAM      append-only, immutable. One node per meaningful change.
  SNAPSHOT SLOTS    fixed cardinality, updated in place. "What is true now",
                    answerable without walking the event log.

  AUDIT LOG         off the graph entirely. Every raw per-window output,
                    verbatim, in a Firestore subcollection. Never rendered,
                    never read in a live decision path.

That last separation is the discipline the whole design rests on: the graph is
the reasoning surface, the audit log is the record. Conflating them is what
produces the flood.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.perception.pipeline import EntityState, PendingEvent, WindowObservation
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.state_tools import apply_state_patch

logger = logging.getLogger(__name__)

SOURCE_AGENT = "perception"


def entity_patch(state: EntityState, source_tool: str) -> tuple[GraphNodePatch, None, str]:
    """The patch for one registry entry, for batching. Same content
    upsert_entity writes — that function is kept for single-write callers."""
    return (
        GraphNodePatch(
            node_id=node_ids.entity(state.stable_id),
            node_type="entity",
            label=state.label,
            attrs={
                "kind": state.kind,
                "first_seen_window": state.first_seen_window,
                "last_seen_window": state.last_seen_window,
                "is_active": state.is_active,
                "observation_count": state.observation_count,
                "confidence_rolling": round(state.confidence_rolling, 3),
                "confidence": round(state.confidence_rolling, 3),
            },
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        None,
        f"{state.label} {'active' if state.is_active else 'inactive'} (seen in {state.observation_count} windows)",
    )


def event_patches(event: PendingEvent, seq: int, obs: WindowObservation, source_tool: str) -> list[tuple]:
    """The event node plus one `involved` edge per entity it references.

    Node first in the returned list, so the batch's consecutive seq assignment
    lands the node before the edges that point at it.
    """
    node_id = node_ids.perception_event(seq, event.kind)
    patches: list[tuple] = [
        (
            GraphNodePatch(
                node_id=node_id,
                node_type="perception_event",
                label=event.label,
                attrs={
                    "event_kind": event.kind,
                    "window_index": obs.window_index,
                    "video_time_s": round(obs.video_time_s, 1),
                    "involved_entity_ids": [node_ids.entity(e) for e in event.involved_entity_ids],
                    "detail": event.detail,
                },
                source_agent=SOURCE_AGENT,
                source_tool=source_tool,
            ),
            None,
            event.label,
        )
    ]
    for stable_id in event.involved_entity_ids:
        entity_node_id = node_ids.entity(stable_id)
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(node_id, entity_node_id, "involved"),
                    source_node_id=node_id,
                    target_node_id=entity_node_id,
                    edge_kind="involved",
                    source_agent=SOURCE_AGENT,
                    source_tool=source_tool,
                    reason=event.label,
                ),
                event.label,
            )
        )
    return patches


def link(source_node_id: str, target_node_id: str, edge_kind: str, reason: str, source_tool: str) -> tuple:
    """One structural edge. Used to give perception's output a spine: the agent
    owns the chain, each activity follows the previous one, and each event
    hangs off the activity it happened during."""
    return (
        None,
        GraphEdgePatch(
            edge_id=node_ids.edge(source_node_id, target_node_id, edge_kind),
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_kind=edge_kind,
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
            reason=reason,
        ),
        reason,
    )


def snapshot_patch(slot_node_id: str, label: str, attrs: dict[str, Any], source_tool: str) -> tuple:
    return (
        GraphNodePatch(
            node_id=slot_node_id,
            node_type="snapshot",
            label=label,
            attrs=attrs,
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        None,
        label,
    )


def phase_patch(phase_id: str, window_index: int, activity_label: str, source_tool: str) -> tuple:
    return (
        GraphNodePatch(
            node_id=node_ids.phase(phase_id, window_index),
            node_type="phase",
            label=activity_label,
            attrs={"opaque_phase_id": phase_id, "window_index": window_index},
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        None,
        f"Phase node for window {window_index}: {activity_label}",
    )


async def upsert_entity(case_id: str, state: EntityState, source_tool: str) -> str:
    """Idempotent registry write.

    Always the same node_id for the same stable_id, so this is an in-place
    update rather than an append — which is exactly what keeps the registry
    bounded at O(distinct objects) instead of growing per window.
    """
    node_id = node_ids.entity(state.stable_id)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="entity",
            label=state.label,
            attrs={
                "kind": state.kind,
                "first_seen_window": state.first_seen_window,
                "last_seen_window": state.last_seen_window,
                "is_active": state.is_active,
                "observation_count": state.observation_count,
                # Smoothed, not the latest raw value — a single occluded frame
                # should nudge this, not overwrite it.
                "confidence_rolling": round(state.confidence_rolling, 3),
                "confidence": round(state.confidence_rolling, 3),
            },
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        reason=f"{state.label} {'active' if state.is_active else 'inactive'} "
        f"(seen in {state.observation_count} windows)",
    )
    return node_id


async def write_event(
    case_id: str, event: PendingEvent, seq: int, obs: WindowObservation, source_tool: str
) -> str:
    """Appends one immutable event node, plus an `involved` edge per entity it
    references.

    The event carries entity IDs and links to them; it never copies entity data
    into its own payload. An event says "this happened involving these things",
    and the things are looked up — so an entity's attributes have exactly one
    home and cannot drift between the registry and a stale event snapshot.
    """
    node_id = node_ids.perception_event(seq, event.kind)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="perception_event",
            label=event.label,
            attrs={
                "event_kind": event.kind,
                "window_index": obs.window_index,
                "video_time_s": round(obs.video_time_s, 1),
                "involved_entity_ids": [node_ids.entity(e) for e in event.involved_entity_ids],
                "detail": event.detail,
            },
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        reason=event.label,
    )

    for stable_id in event.involved_entity_ids:
        entity_node_id = node_ids.entity(stable_id)
        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=node_ids.edge(node_id, entity_node_id, "involved"),
                source_node_id=node_id,
                target_node_id=entity_node_id,
                edge_kind="involved",
                source_agent=SOURCE_AGENT,
                source_tool=source_tool,
                reason=event.label,
            ),
            reason=event.label,
        )
    return node_id


async def update_snapshot_slot(case_id: str, slot_node_id: str, label: str, attrs: dict[str, Any], source_tool: str) -> None:
    """In-place write to one of the fixed slots. Cardinality never grows with
    case length — that is the entire point of the snapshot tier."""
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=slot_node_id,
            node_type="snapshot",
            label=label,
            attrs=attrs,
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        reason=label,
    )


async def write_phase_node(case_id: str, phase_id: str, window_index: int, activity_label: str, source_tool: str) -> str:
    """A phase node labeled with a real semantic description.

    The opaque numeric phase id keys node IDENTITY so the same real segment
    does not fragment across windows; the LABEL is always an agent's own words.
    A node reading "Phase 3" would carry no information a viewer could use.
    """
    node_id = node_ids.phase(phase_id, window_index)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="phase",
            label=activity_label,
            attrs={"opaque_phase_id": phase_id, "window_index": window_index},
            source_agent=SOURCE_AGENT,
            source_tool=source_tool,
        ),
        reason=f"Phase node for window {window_index}: {activity_label}",
    )
    return node_id


async def write_audit_record(case_id: str, obs: WindowObservation, raw_output: Any, suppressed: list[str]) -> None:
    """Full-fidelity raw output, off the graph (§7.9).

    Fire-and-forget by design: the audit log is for post-hoc analysis and
    benchmark alignment, never a live decision path, so a failure to record it
    must not stall the sweep or lose the window. It is logged, not swallowed.

    Also records WHAT WAS SUPPRESSED and why. Without that, a window where the
    pipeline correctly held an event back is indistinguishable in the record
    from a window where the model saw nothing — and debugging a debounce rule
    against a log that cannot tell those apart is guesswork.
    """
    from services.state_service import store  # local import: keeps this module importable without the service

    record = {
        "window_index": obs.window_index,
        "video_time_s": obs.video_time_s,
        "raw_output": raw_output.model_dump() if hasattr(raw_output, "model_dump") else raw_output,
        "suppressed": suppressed,
    }
    try:
        await store.write_perception_audit(case_id, obs.window_index, record)
    except Exception:
        logger.exception("perception audit write failed for %s window %s", case_id, obs.window_index)
