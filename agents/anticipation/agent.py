"""Anticipation Agent's public entry point — what Orchestrator calls.

Plan §13.3, second revision. Gemini never sees or produces a numeric phase
ID — it only ever sees real video stills and free-text context, and
answers entirely in its own words (agents/anticipation/subagent.py's
`_INSTRUCTION`/`AnticipationOutput` have no numeral anywhere). The real
opaque numeric phase ID (`tools/action_labels.py::phase_at_frame`) still
exists, but demoted to a purely internal bookkeeping key the WRAPPER (this
module, plain Python) uses for two things only, neither ever reaching
Gemini or the UI:

  1. Graph node identity — `phase:{numeric_id}` stays the node_id so the
     same real segment doesn't fragment into separate nodes across windows
     and agents (the same already-established §12 pattern), but the
     node's LABEL now comes from an agent's own real semantic description,
     never `f"Phase {id}"`.
  2. An anonymized statistics hint for forecasting — the wrapper calls
     tools/phase_transition_priors.py::summarize_transition_confidence
     directly (no longer a Gemini-callable tool: Gemini has no legitimate
     way to know which numeric key to pass), embedding only the real
     confidence SHAPE ("historically very consistent" vs "ambiguous") in
     the prompt, never a category label.

Dead reckoning (docs/latency_optimization.md): runs on the same fixed
real-time window cadence as Scene Graph Builder, ahead of Error Detection/Scene
Graph Builder's own slower real observations. Convergence: since Gemini's
predicted next-phase is now free text, not a numeral, matching a
prediction against a later real observation is done via `_labels_match`
(slugified exact/substring match, not semantic equivalence — a real,
disclosed limitation, see plan §13.3) rather than node_id equality.
`_reconcile_pending` polls the live graph (tools/state_tools.get_state_snapshot)
and, on a match, REDIRECTS the predicted edge's target to the real node an
independent agent actually wrote — a dashed edge visibly landing on the
same real node Scene Graph Builder/Error Detection populated, not two disconnected
placeholders sitting side by side.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agents.anticipation.subagent import AnticipationOutput, build_subagent
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.action_labels import load_action_segments, phase_at_frame
from tools.adk_runner import run_llm_agent_once
from tools.phase_transition_priors import summarize_transition_confidence
from tools.state_tools import apply_state_patch, get_state_snapshot
from tools.video_utils import (
    DEFAULT_WINDOW_S,
    VideoWindow,
    build_multimodal_content,
    find_video_fps,
    find_video_path,
    format_video_time_range,
    generate_nonoverlapping_windows,
    sample_frames,
)

# Public singleton — Orchestrator's sub_agents= declaration builds its own
# fresh instance (build_subagent()) from this same module, matching the
# ADK-multi-parent-safe pattern already used elsewhere.
AGENT = build_subagent()

_GEMINI_CONCURRENCY = asyncio.Semaphore(6)

_WINDOW_S = DEFAULT_WINDOW_S  # shared, config-driven — matches Error Detection's/Scene Graph Builder's own real-time cadence
_STILL_FRAME_COUNT = 4
_STILL_RESIZE = (960, 540)

# Real agents whose phase-node writes count as independent corroboration —
# never "anticipation" itself.
_REAL_OBSERVING_AGENTS = {"scene_graph_builder", "error_detection_coordinator"}

_RECONCILE_POLL_INTERVAL_S = 5.0
_RECONCILE_MAX_POLLS = 24  # ~120s bounded wait — Error Detection's own sweep is the real long pole anyway

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
VALIDATION_LOG_PATH = DATA_ROOT / "validation" / "anticipation_accuracy.jsonl"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_")


def _labels_match(predicted_label: str, real_label: str) -> bool:
    """Real, disclosed limitation (plan §13.3): exact/substring match on
    slugified text, not semantic equivalence. Two agents phrasing the
    "same" real phase differently can fail to converge on wording alone —
    a semantic-equivalence LLM check is a reasonable stretch improvement,
    not required for this build."""
    p, r = _slugify(predicted_label), _slugify(real_label)
    if not p or not r:
        return False
    return p == r or p in r or r in p


@dataclass(frozen=True)
class PendingForecast:
    edge_id: str
    predicted_label: str


async def _ensure_agent_node(case_id: str) -> None:
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id="agent:anticipation",
            node_type="agent",
            label="Anticipation Agent",
            source_agent="anticipation",
            source_tool="anticipate_case",
        ),
        reason="Anticipation Agent registered for this case",
    )


async def _forecast_window(video_id: str, window: VideoWindow, hint_text: str) -> AnticipationOutput:
    video_path = find_video_path(video_id)
    if video_path is None:
        raise FileNotFoundError(f"no source video found for {video_id!r}")
    frames = sample_frames(video_path, window.start_frame, window.end_frame, n_frames=_STILL_FRAME_COUNT, resize_to=_STILL_RESIZE)
    instruction_text = (
        f"Current window: video seconds {window.start_s:.1f}-{window.end_s:.1f}.\n"
        f"Historical timing context (no category names — see instructions): {hint_text}"
    )
    content = build_multimodal_content(instruction_text, frames)
    async with _GEMINI_CONCURRENCY:
        return await run_llm_agent_once(AGENT, content, AnticipationOutput, app_name="surggraph_anticipation")


def _log_anticipation_accuracy(video_id: str, case_id: str, window: VideoWindow, real_numeric_id: str | None, forecast: AnticipationOutput) -> None:
    """VALIDATION-ONLY: `real_numeric_id` (real ground truth) is logged
    purely for offline scoring — never shown to Gemini, never used to
    decide anything on the live graph (see module docstring). No name
    legend exists for this dataset, so there's no ground truth to score
    "was the semantic name right" against directly; what IS honestly
    scoreable from this log (scripts/summarize_anticipation_accuracy.py):
    change-point accuracy (does Gemini's own description change when the
    real numeric id changes between consecutive windows) and self-
    consistency (does the same real id tend to get similar descriptions)."""
    VALIDATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "video_id": video_id,
        "case_id": case_id,
        "window_id": window.window_id,
        "window_start_s": round(window.start_s, 2),
        "window_end_s": round(window.end_s, 2),
        "real_numeric_phase_id": real_numeric_id,
        "gemini_current_phase_name": forecast.current_phase_name,
        "gemini_current_phase_confidence": forecast.current_phase_confidence,
        "gemini_next_phase_name": forecast.next_phase_name,
        "gemini_next_phase_confidence": forecast.next_phase_confidence,
        "gemini_eta_seconds": forecast.eta_seconds,
    }
    with open(VALIDATION_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


async def _write_current_phase_node(
    case_id: str, real_numeric_id: str | None, phase_name: str, confidence: float, time_range_label: str
) -> str:
    """Returns the node_id written to (or that would anchor the current
    phase) — `phase:{real_numeric_id}` when the window falls inside a real
    annotated segment; otherwise Anticipation's own text becomes the key
    too (a real, disclosed annotation-coverage gap, not fabricated)."""
    node_id = f"phase:{real_numeric_id}" if real_numeric_id is not None else f"predicted-phase:{_slugify(phase_name)}"
    await apply_state_patch(
        case_id,
        node=GraphNodePatch(
            node_id=node_id,
            node_type="phase",
            label=f"{phase_name} ({time_range_label})",
            attrs={"confidence": confidence},
            source_agent="anticipation",
            source_tool="anticipate_case",
        ),
        reason=f"Anticipation's own live read of the current window (confidence={confidence:.2f})",
    )
    return node_id


async def _write_forecast(case_id: str, source_node_id: str, forecast: AnticipationOutput, window: VideoWindow) -> PendingForecast | None:
    """Writes the predicted edge. If a real agent has ALREADY independently
    written a phase node whose label matches the forecast (a real race,
    given all sweeps run concurrently), the edge targets that real node
    directly, already confirmed — no placeholder needed."""
    predicted_label = forecast.next_phase_name
    snapshot = await get_state_snapshot(case_id)
    real_match = next(
        (
            n
            for n in snapshot.nodes
            if n.node_type == "phase" and n.source_agent in _REAL_OBSERVING_AGENTS and _labels_match(predicted_label, n.label)
        ),
        None,
    )

    if real_match is not None:
        target_node_id = real_match.node_id
        edge_kind, confirmation_signal = "confirmation", "confirmed"
    else:
        target_node_id = node_ids.predicted_phase(predicted_label)
        edge_kind, confirmation_signal = "prediction", "pending"
        await apply_state_patch(
            case_id,
            node=GraphNodePatch(
                node_id=target_node_id,
                node_type="phase",
                label=f"{predicted_label} (predicted, not yet observed)",
                source_agent="anticipation",
                source_tool="anticipate_case",
            ),
            reason="Anticipation forecast target — not yet independently observed",
        )

    trajectory_id = f"anticipation-{window.window_id}"
    edge_id = f"edge:{trajectory_id}"
    await apply_state_patch(
        case_id,
        edge=GraphEdgePatch(
            edge_id=edge_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_kind=edge_kind,
            trajectory_id=trajectory_id,
            confirmation_signal=confirmation_signal,
            reason=(
                f"{forecast.reasoning} (current={forecast.current_phase_name!r}@{forecast.current_phase_confidence:.2f}, "
                f"next={forecast.next_phase_name!r}@{forecast.next_phase_confidence:.2f}, ETA={forecast.eta_seconds:.0f}s)"
            ),
            source_agent="anticipation",
            source_tool="anticipate_case",
        ),
        reason=forecast.reasoning,
        source_agent="anticipation",
        source_tool="anticipate_case",
    )
    if real_match is not None:
        return None
    return PendingForecast(edge_id=edge_id, predicted_label=predicted_label)


async def _reconcile_pending(case_id: str, pending: list[PendingForecast]) -> None:
    """Bounded live-graph poll: on a real independent match, REDIRECTS the
    predicted edge's target to the real node (not just a label restyle) —
    the genuine "converges with the actual branch" visual: the dashed edge
    ends up pointing at the exact same node Scene Graph Builder/Error Detection
    populated. Never resolves to "refuted" — without a matching real
    observation this stays honestly "pending" rather than a live ground-
    truth check masquerading as a live decision."""
    remaining = {p.edge_id: p for p in pending}
    if not remaining:
        return
    for _ in range(_RECONCILE_MAX_POLLS):
        await asyncio.sleep(_RECONCILE_POLL_INTERVAL_S)
        snapshot = await get_state_snapshot(case_id)
        real_phase_nodes = [n for n in snapshot.nodes if n.node_type == "phase" and n.source_agent in _REAL_OBSERVING_AGENTS]
        edge_by_id = {e.edge_id: e for e in snapshot.edges}

        newly_confirmed = []
        for edge_id, forecast_target in remaining.items():
            match = next((n for n in real_phase_nodes if _labels_match(forecast_target.predicted_label, n.label)), None)
            if match is not None:
                newly_confirmed.append((edge_id, match))

        for edge_id, match in newly_confirmed:
            remaining.pop(edge_id, None)
            edge = edge_by_id.get(edge_id)
            if edge is None:
                continue
            await apply_state_patch(
                case_id,
                edge=edge.model_copy(
                    update={
                        "target_node_id": match.node_id,
                        "edge_kind": "confirmation",
                        "confirmation_signal": "confirmed",
                        "timestamp": datetime.now(timezone.utc),
                    }
                ),
                reason=f"Independently corroborated — a real agent's own observation ({match.label!r}) matches this forecast",
                source_agent="anticipation",
                source_tool="anticipate_case",
            )
        if not remaining:
            return


async def anticipate_case(
    case_id: str, video_id: str, start_s: float = 0.0, end_s: float | None = None
) -> list[AnticipationOutput]:
    """Runs the live Anticipation forecast sweep over [start_s, end_s) of
    `video_id`'s real fixed-cadence windows. `async def` — Orchestrator
    awaits this directly on its own shared event loop alongside
    error_detection_case and scene_graph_case, same pattern as those two."""
    await _ensure_agent_node(case_id)
    fps = find_video_fps(video_id)
    if fps is None:
        raise ValueError(f"no source video found for {video_id!r} — cannot derive real timestamps for the graph")
    segments = load_action_segments(video_id)
    if end_s is None:
        end_s = segments[-1].end_frame / fps
    windows = generate_nonoverlapping_windows(start_s, end_s, _WINDOW_S, fps, id_prefix="anticipation")

    forecasts: list[AnticipationOutput] = []
    pending: list[PendingForecast] = []

    async def process(window: VideoWindow):
        real_numeric_id = phase_at_frame(video_id, window.start_frame, segments=segments)
        hint_text = (
            summarize_transition_confidence(real_numeric_id)
            if real_numeric_id is not None
            else "No historical transition data is available (outside any annotated segment)."
        )
        forecast = await _forecast_window(video_id, window, hint_text)
        return window, real_numeric_id, forecast

    tasks = [asyncio.ensure_future(process(w)) for w in windows]
    for coro in asyncio.as_completed(tasks):
        window, real_numeric_id, forecast = await coro
        forecasts.append(forecast)
        _log_anticipation_accuracy(video_id, case_id, window, real_numeric_id, forecast)

        time_range_label = format_video_time_range(window.start_s, window.end_s)
        current_node_id = await _write_current_phase_node(
            case_id, real_numeric_id, forecast.current_phase_name, forecast.current_phase_confidence, time_range_label
        )

        forecast_target = await _write_forecast(case_id, current_node_id, forecast, window)
        if forecast_target is not None:
            pending.append(forecast_target)

    await _reconcile_pending(case_id, pending)
    return forecasts
