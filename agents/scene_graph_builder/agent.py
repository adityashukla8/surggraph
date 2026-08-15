"""Scene Graph Builder's public entry point — what Orchestrator calls.

Populates the graph with real `entity` nodes (instruments/anatomy) and
real relation edges — the piece the Living State Graph has been missing
since Monitor Agent detects divergence but never extracts scene content.
Runs independently of Monitor (Orchestrator kicks both off concurrently
over the same real video-duration sweep — see agents/orchestrator/agent.py)
and streams real-time traceability the same way: every window's real
findings become visible the moment that window finishes, not batched at
the end.

Windowing is simple, non-overlapping chunks (not Monitor's heavy-overlap
CARES-style windows) — scene composition changes slower than error
detection needs, and it's meaningfully cheaper.

Ground-truth-as-input-context (plan §12): each window's real phase/action
ID is fetched here and passed as input context to the Gemini call —
standing in for what a genuine upstream perception/telemetry system would
provide, never used to shortcut the agent's own entity-naming/relation
output, which stays live reasoning (agents/scene_graph_builder/subagent.py's
instruction is explicit about this to the model too). The real
segmentation-mask image was dropped from this input as of
docs/latency_optimization.md's restructuring — a real, confirmed-expensive
per-image tiling cost with no matching latency benefit here.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pydantic import BaseModel

from agents.scene_graph_builder.subagent import SceneGraphWindowOutput, build_subagent
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.adk_runner import run_llm_agent_once
from tools.state_tools import apply_state_patch
from tools.video_utils import (
    DEFAULT_WINDOW_S,
    build_multimodal_content,
    find_video_duration_s,
    find_video_fps,
    find_video_path,
    format_video_time_range,
    generate_nonoverlapping_windows,
    sample_frames,
)

# One static instance, reused across windows (the instruction is role-only,
# not window-specific — matches Monitor's _SCREEN_AGENTS pattern). Public
# (no leading underscore) since Orchestrator reuses this exact instance
# for its own sub_agents= declaration, the same "Registry sees the real
# agent that actually gets invoked" pattern MonitorCoordinatorAgent uses.
AGENT = build_subagent()

# Own, separate concurrency budget from Monitor's _GEMINI_CONCURRENCY —
# Orchestrator runs both agents' sweeps at the same time over the same
# real video duration, and each bounds only its own call stream.
_GEMINI_CONCURRENCY = asyncio.Semaphore(6)

# Still frames, not native video (docs/latency_optimization.md's second
# latency pass — native video's per-call cost is dominated by GCS fetch +
# server-side decode, not clip length, and was a real contributor to
# nothing meaningful showing on the graph for ~2 minutes of real video
# playback). Native resolution (no resize) — unlike Monitor's temporal
# role, SGB's job is naming small instruments/anatomy correctly, which
# benefits from full detail more than it needs motion-dense sampling.
_STILL_FRAME_COUNT = 5
_STILL_RESIZE = None


class SceneGraphWindow(BaseModel):
    window_id: str
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int


def _generate_windows(start_s: float, end_s: float, window_s: float, fps: float) -> list[SceneGraphWindow]:
    return [
        SceneGraphWindow(window_id=w.window_id, start_s=w.start_s, end_s=w.end_s, start_frame=w.start_frame, end_frame=w.end_frame)
        for w in generate_nonoverlapping_windows(start_s, end_s, window_s, fps, id_prefix="scenegraph")
    ]


async def run_scene_graph_window(video_id: str, window: SceneGraphWindow, phase_action_id: str | None) -> SceneGraphWindowOutput:
    video_path = find_video_path(video_id)
    if video_path is None:
        raise FileNotFoundError(f"no source video found for {video_id!r}")

    instruction_text = (
        f"Analyze this ~{window.end_s - window.start_s:.0f}s window "
        f"(video seconds {window.start_s:.1f}-{window.end_s:.1f})."
    )
    if phase_action_id is not None:
        instruction_text += f" Real phase-classification signal for this window: action_id={phase_action_id!r}."
    else:
        instruction_text += " No real phase signal is available for this window."

    frames = sample_frames(
        video_path,
        start_frame=window.start_frame,
        end_frame=window.end_frame,
        n_frames=_STILL_FRAME_COUNT,
        resize_to=_STILL_RESIZE,
    )
    content = build_multimodal_content(instruction_text=instruction_text, frames=frames)
    async with _GEMINI_CONCURRENCY:
        return await run_llm_agent_once(AGENT, content, SceneGraphWindowOutput, app_name="surggraph_scene_graph_builder")


async def run_scene_graph_sweep(
    video_id: str,
    start_s: float = 0.0,
    end_s: float | None = None,
    window_s: float = DEFAULT_WINDOW_S,
    on_window_complete: Callable[[SceneGraphWindow, SceneGraphWindowOutput], Awaitable[None]] | None = None,
) -> list[tuple[SceneGraphWindow, SceneGraphWindowOutput]]:
    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no source video found for {video_id!r}")
    if end_s is None:
        end_s = find_video_duration_s(video_id)
        if end_s is None:
            raise ValueError(f"could not determine real duration for {video_id!r} — pass end_s explicitly")

    segments = load_action_segments(video_id)
    windows = _generate_windows(start_s, end_s, window_s, fps)

    async def process(window: SceneGraphWindow) -> tuple[SceneGraphWindow, SceneGraphWindowOutput]:
        mid_frame = (window.start_frame + window.end_frame) // 2
        phase_action_id = phase_at_frame(video_id, mid_frame, segments=segments)
        output = await run_scene_graph_window(video_id, window, phase_action_id)
        return window, output

    tasks = [asyncio.ensure_future(process(w)) for w in windows]
    results: list[tuple[SceneGraphWindow, SceneGraphWindowOutput]] = []
    for coro in asyncio.as_completed(tasks):
        window, output = await coro
        results.append((window, output))
        if on_window_complete is not None:
            await on_window_complete(window, output)
    return results


async def _ensure_agent_node(case_id: str) -> None:
    """Real bug fix: this registration write existed before an earlier
    rewrite this session dropped it — without it, "Scene Graph Builder"
    never appeared on the graph until its FIRST window's real Gemini call
    returned (via _widen_agent_time_range, which only fires on window
    completion), unlike Monitor's/Anticipation's own agent nodes, which
    register immediately. Matches those agents' own pattern: idempotent,
    no time range yet (real windows haven't landed)."""
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id="agent:scene_graph_builder",
            node_type="agent",
            label="Scene Graph Builder",
            source_agent="scene_graph_builder",
            source_tool="scene_graph_case",
        ),
        reason="Scene Graph Builder registered for this case",
    )


async def _widen_agent_time_range(
    case_id: str, window_start_s: float, window_end_s: float, agent_range: list[float]
) -> None:
    """Widens (never shrinks) the persistent agent node's real cumulative
    time range to cover this window too, re-writing the node's label.
    `agent_range` is a 2-element [min_start, max_end] list (mutable box,
    since a single Scene Graph Builder agent node — unlike Monitor's four
    — doesn't need a dict keyed by node_id)."""
    if not agent_range:
        agent_range[:] = [window_start_s, window_end_s]
    else:
        agent_range[0] = min(agent_range[0], window_start_s)
        agent_range[1] = max(agent_range[1], window_end_s)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id="agent:scene_graph_builder",
            node_type="agent",
            label=f"Scene Graph Builder ({format_video_time_range(*agent_range)})",
            source_agent="scene_graph_builder",
            source_tool="scene_graph_case",
        ),
        reason=f"Scene Graph Builder processed window {format_video_time_range(window_start_s, window_end_s)}",
    )


async def _ensure_phase_node(
    case_id: str, phase: str, phases_seen: set[str], time_range_label: str, activity_description: str
) -> None:
    """Label is the window's own real, live `activity_description` (plan
    §13.4) — Scene Graph Builder's actual output for that window, not the
    generic `"Phase {id}"` fallback Monitor uses when it has nothing better
    to offer. Idempotent — writes once per newly-seen phase, first
    real description wins (harmless if Anticipation's own live naming for
    the same real segment lands first or after; both are equally real,
    disclosed last-write-wins, see agents/anticipation/agent.py)."""
    if phase in phases_seen:
        return
    phases_seen.add(phase)
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=f"phase:{phase}",
            node_type="phase",
            label=f"{activity_description} ({time_range_label})",
            source_agent="scene_graph_builder",
            source_tool="scene_graph_case",
        ),
        reason=f"Phase {phase} first referenced",
    )


async def _upsert_entity_node(
    case_id: str,
    entity,
    window_start_s: float,
    window_end_s: float,
    entity_ranges: dict[str, tuple[float, float]],
    reason: str,
) -> None:
    """Entity nodes are real, tracked real-world objects (plan §12: the
    agent assigns a stable entity_id so the same object persists across
    windows) — so like agent nodes, their time range is the real
    cumulative span of windows they've actually been observed in, widened
    (never shrunk) each time the same entity_id reappears."""
    prev = entity_ranges.get(entity.entity_id)
    new_range = (window_start_s, window_end_s) if prev is None else (min(prev[0], window_start_s), max(prev[1], window_end_s))
    entity_ranges[entity.entity_id] = new_range
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=f"entity:{entity.entity_id}",
            node_type="entity",
            label=f"{entity.label} ({format_video_time_range(*new_range)})",
            attrs={"entity_type": entity.entity_type, "confidence": entity.confidence},
            source_agent="scene_graph_builder",
            source_tool="run_scene_graph_window",
        ),
        reason=reason,
        source_agent="scene_graph_builder",
        source_tool="run_scene_graph_window",
    )


async def scene_graph_case(
    case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None
) -> list[SceneGraphWindowOutput]:
    """Runs the live Scene Graph Builder sweep over [start_s, end_s) of
    `video_id`, writing real entity nodes and relation edges to the graph
    as each window completes. `async def` — Orchestrator awaits this
    directly on its own shared event loop, same pattern as monitor_case."""
    await _ensure_agent_node(case_id)
    segments = load_action_segments(video_id)
    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no source video found for {video_id!r} — cannot derive real timestamps for the graph")
    phases_seen: set[str] = set()
    agent_range: list[float] = []
    entity_ranges: dict[str, tuple[float, float]] = {}
    outputs: list[SceneGraphWindowOutput] = []

    async def on_window_complete(window: SceneGraphWindow, output: SceneGraphWindowOutput) -> None:
        outputs.append(output)
        phase = phase_at_frame(video_id, window.start_frame, segments=segments) or "unknown"
        segment = next((s for s in segments if s.start_frame <= window.start_frame <= s.end_frame), None)
        phase_time_range = (
            format_video_time_range(segment.start_frame / fps, segment.end_frame / fps) if segment is not None else "time unknown"
        )
        await _ensure_phase_node(case_id, phase, phases_seen, phase_time_range, output.activity_description)
        await _widen_agent_time_range(case_id, window.start_s, window.end_s, agent_range)

        # Real-time traceability: every real entity this window found
        # becomes a visible node the moment the window finishes — entity
        # nodes are idempotently upserted per entity_id, so the same
        # real-world object persists as one node across windows rather
        # than duplicating.
        for entity in output.entities:
            await _upsert_entity_node(case_id, entity, window.start_s, window.end_s, entity_ranges, output.reasoning)

        await apply_state_patch(
            case_id,
            edge=GraphEdgePatch(
                edge_id=f"edge:sg-{window.window_id}",
                source_node_id="agent:scene_graph_builder",
                target_node_id=f"phase:{phase}",
                edge_kind="action",
                source_agent="scene_graph_builder",
                source_tool="run_scene_graph_window",
                reason=output.activity_description,
            ),
            reason=output.activity_description,
            source_agent="scene_graph_builder",
            source_tool="run_scene_graph_window",
        )

        for i, relation in enumerate(output.relations):
            if relation.target_entity_id is None:
                continue  # a relation with no real target isn't a graph edge
            await apply_state_patch(
                case_id,
                edge=GraphEdgePatch(
                    edge_id=f"edge:sg-relation-{window.window_id}-{i}",
                    source_node_id=f"entity:{relation.subject_entity_id}",
                    target_node_id=f"entity:{relation.target_entity_id}",
                    edge_kind="action",
                    source_agent="scene_graph_builder",
                    source_tool="run_scene_graph_window",
                    reason=relation.verb,
                ),
                reason=relation.verb,
                source_agent="scene_graph_builder",
                source_tool="run_scene_graph_window",
            )

    await run_scene_graph_sweep(video_id, start_s=start_s, end_s=end_s, on_window_complete=on_window_complete)
    return outputs
