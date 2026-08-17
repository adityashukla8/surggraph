"""Links a detected error to the activity it happened during.

docs/plan_v2 §4.2 defines the `detection` edge as perception -> error, and that
relation was missing: Error Detection anchored its errors to its own agent
node, so the graph held two parallel trees that met only at the trigger. An
error at 0:20 had no link to what was being done at 0:20, even though both
sides carry a real timestamp and a real window index.

DERIVED, NOT GUESSED. Both agents window the same video at the same
config-driven size, so the activity in effect at an error's real video time is
a lookup, not an inference: the latest activity perception had observed at or
before that moment. Nothing is estimated and no threshold is invented — if no
activity had been observed yet, no edge is written.

NEVER WRITES A DANGLING EDGE. The two sweeps run concurrently and independently,
so at the instant an error is written the covering phase node may not exist yet.
The rule is simple: link only what is genuinely on the graph now. Anything
missed is picked up by the reconciliation pass at case close, once both sweeps
have finished and every phase node exists. That is why this is safe to add to a
pipeline that already works — it can only ever add an edge between two nodes
that both exist, and never blocks or alters the detection itself.
"""

from __future__ import annotations

import logging

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateSnapshot
from tools.state_tools import apply_state_patches, get_state_snapshot
from tools.video_utils import DEFAULT_WINDOW_S

logger = logging.getLogger(__name__)

SOURCE_AGENT = "error_detection_coordinator"
_SOURCE_TOOL = "link_error_to_activity"


def covering_phase(snapshot: StateSnapshot, video_time_s: float, window_s: float = DEFAULT_WINDOW_S) -> GraphNodePatch | None:
    """The activity IN EFFECT at `video_time_s`.

    Perception writes a phase node when the phase CHANGES, not once per window,
    so a case with eighteen windows may hold eleven phase nodes. An activity
    observed at window 2 remains the activity through windows 3, 4 and 5 until
    the next change — which is why this takes the latest phase node that began
    at or before the moment in question, rather than demanding one whose own
    window contains it.

    An earlier version required that exact match and linked only four of nine
    real errors; the other five fell in windows where nothing had changed, so
    no phase node carried their index. The activity was perfectly well known at
    those moments, just recorded earlier.

    Returns None only when the error precedes any observed activity — early in
    a case, before perception's first window has returned. Nothing is invented
    to fill that: an unlinked error is honest, a link to an activity that had
    not been observed yet would not be.
    """
    best: GraphNodePatch | None = None
    best_start = -1.0

    for node in snapshot.nodes:
        if node.node_type != "phase":
            continue
        index = node.attrs.get("window_index")
        if not isinstance(index, int):
            continue
        start = index * window_s
        # Latest activity that had already begun. Strictly at-or-before, so a
        # later phase can never be credited with an earlier error.
        if start <= video_time_s and start > best_start:
            best, best_start = node, start

    return best


def _edge(phase_node_id: str, error_node_id: str, reason: str) -> tuple:
    return (
        None,
        GraphEdgePatch(
            edge_id=node_ids.edge(phase_node_id, error_node_id, "detection"),
            source_node_id=phase_node_id,
            target_node_id=error_node_id,
            edge_kind="detection",
            source_agent=SOURCE_AGENT,
            source_tool=_SOURCE_TOOL,
            reason=reason,
        ),
        reason,
    )


async def link_error(case_id: str, error_node_id: str, video_time_s: float, snapshot: StateSnapshot | None = None) -> str | None:
    """Links one error to its covering activity, if that activity exists yet.

    Returns the phase node id it linked to, or None. Takes an optional
    already-fetched snapshot so a caller writing several errors for one window
    does not re-read the graph per error.
    """
    snapshot = snapshot or await get_state_snapshot(case_id)
    phase = covering_phase(snapshot, video_time_s)
    if phase is None:
        return None

    await apply_state_patches(case_id, [_edge(phase.node_id, error_node_id, f"Detected during: {phase.label}")])
    return phase.node_id


async def reconcile(case_id: str) -> int:
    """Links any errors still missing an activity, after both sweeps finish.

    Perception and Error Detection run concurrently at different speeds, so an
    error written before its window's phase node existed could not be linked at
    the time. This runs at case close, when every phase node is present, and
    returns how many links it added.
    """
    snapshot = await get_state_snapshot(case_id)

    already_linked = {
        e.target_node_id
        for e in snapshot.edges
        if e.edge_kind == "detection" and e.source_node_id.startswith("phase:")
    }

    patches: list[tuple] = []
    for node in snapshot.nodes:
        if node.node_type != "error" or node.node_id in already_linked:
            continue
        video_time_s = node.attrs.get("video_time_s")
        if not isinstance(video_time_s, (int, float)):
            continue
        phase = covering_phase(snapshot, float(video_time_s))
        if phase is None:
            # No activity was observed covering this moment — perception may
            # have failed that window. Recorded as nothing rather than linked
            # to the nearest available phase, which would assert a context
            # that was never observed.
            continue
        patches.append(_edge(phase.node_id, node.node_id, f"Detected during: {phase.label}"))

    if patches:
        await apply_state_patches(case_id, patches)
    logger.info("phase_link[%s]: linked %d error(s) to their activity at close", case_id, len(patches))
    return len(patches)
