"""Complication Reasoning Agent — event-driven off error nodes.

docs/agentic_workflow.md §3 agent 4, docs/plan_v2 §6 step 4. The first
event-driven agent in the system: it does not sweep, it subscribes. When Error
Detection writes an error node above the severity threshold, the bus invokes
this, and nothing happens otherwise.

    error node (severity >= medium)
        -> formulate a literature query from live case context
        -> Literature Retrieval fires a real Europe PMC call
        -> reason over the retrieved abstracts + the patient + the vitals
        -> complication nodes, each with a causal_reasoning edge back to the
           error and evidence edges from the papers that support it

THE CHAIN IS THE DELIVERABLE. A complication node that does not connect back to
its triggering error, or forward to the evidence behind it, is an assertion
rather than a piece of reasoning — and the verification gate downstream is
specified to refuse an external write whose chain has a hole in it. So the
edges here are not decoration; they are what makes the claim auditable.
"""

from __future__ import annotations

import logging

from agents.complication_reasoning.subagent import (
    ComplicationAssessment,
    LiteratureQuery,
    build_query_agent,
    build_reasoning_agent,
)
from agents.error_detection.severity import meets_complication_trigger
from agents.literature_retrieval.agent import _clean as clean_literature_text, evidence_edges, retrieve
from google.genai import types

from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent
from tools.adk_runner import run_llm_agent_once
from tools.context_slice import GraphIndex, complication_reasoning as complication_slice
from tools.medgemma_model import fire_shadow_latency_call
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "complication_reasoning"
_SOURCE_TOOL = "reason_about_complications"

QUERY_AGENT = build_query_agent()
REASONING_AGENT = build_reasoning_agent()

# Cache key per docs §9: the same category, in the same phase, for the same
# patient is the same question — re-reasoning it costs two Gemini calls and a
# network fetch to arrive at the answer already on the graph.
_seen: set[tuple[str, str, str, str]] = set()


def _as_content(text: str) -> types.Content:
    """These agents are text-only (no frames), but ADK's runner still wants a
    real Content rather than a bare string."""
    return types.Content(role="user", parts=[types.Part(text=text)])


def _format_slice(slice_context: dict, retrieved: list[dict] | None = None) -> str:
    """Renders a context slice as prompt text.

    Prose rather than raw JSON: this goes into a clinical-reasoning prompt, and
    a readable rendering costs fewer tokens than a nested object while being
    easier for the model to actually use.
    """
    error = slice_context.get("triggering_error") or {}
    twin = slice_context.get("patient_twin") or {}
    vitals = slice_context.get("vitals_trend") or {}
    phase = slice_context.get("current_phase") or {}
    recent = slice_context.get("recent_errors") or []

    lines = [
        "DETECTED ERROR",
        f"  {error.get('label', '(unknown)')}",
        f"  category: {error.get('attrs', {}).get('error_category')}, "
        f"severity: {error.get('attrs', {}).get('severity_band')} "
        f"({error.get('attrs', {}).get('severity')})",
        f"  detector's reasoning: {error.get('attrs', {}).get('reasoning', '(none recorded)')}",
        "",
        "PATIENT (synthetic, illustrative)",
        f"  {twin.get('attrs', {}).get('prompt_summary', '(no profile loaded)')}",
        "",
        "CURRENT ACTIVITY",
        f"  {phase.get('label', '(unknown)')}",
        "",
        "VITALS",
        f"  {vitals.get('attrs', {}).get('excursion_label') or 'within expected range for this point in the case'}",
        f"  HR {vitals.get('attrs', {}).get('hr_bpm')}, MAP {vitals.get('attrs', {}).get('map_mmhg')}, "
        f"SpO2 {vitals.get('attrs', {}).get('spo2_pct')}, EtCO2 {vitals.get('attrs', {}).get('etco2_mmhg')}",
    ]

    other = [e for e in recent if e.get("id") != error.get("id")]
    if other:
        lines += ["", "RECENT OTHER ERRORS THIS CASE"]
        lines += [f"  - {e['label']}" for e in other[:5]]

    if retrieved is not None:
        lines += ["", "RETRIEVED LITERATURE"]
        if not retrieved:
            lines.append("  (none — retrieval returned no results; reason from general knowledge and mark candidates unsupported)")
        for i, hit in enumerate(retrieved):
            lines.append(f"  [{i}] {clean_literature_text(hit.get('title')) or '(untitled)'}")
            snippet = clean_literature_text(hit.get("snippet"))
            if snippet:
                lines.append(f"      {snippet[:500]}")

    return "\n".join(lines)


async def _record_outcome(case_id: str, error_node, status: str, detail: str) -> None:
    """Writes back WHY an error did or did not produce complications.

    Without this the graph silently omits. Two errors look identical on screen,
    one with complications hanging off it and one with none, and there is no
    way to tell whether it was reasoned and found benign, skipped as a repeat
    of a category already covered in this phase, or below the severity
    threshold. Recording the outcome is what makes the absence readable instead
    of looking like a dropped step.
    """
    await apply_state_patches(
        case_id,
        [
            (
                error_node.model_copy(
                    update={"attrs": {**error_node.attrs, "complication_status": status, "complication_status_detail": detail}}
                ),
                None,
                f"Complication reasoning: {status}",
            )
        ],
    )


async def reason_about_error(case_id: str, error_node_id: str) -> list[str]:
    """Runs the full chain for one error node. Returns the complication node ids
    written (empty when the error genuinely warrants none)."""
    index = GraphIndex(await get_state_snapshot(case_id))
    error_node = index.nodes_by_id.get(error_node_id)
    if error_node is None:
        logger.warning("complication[%s]: %s not on the graph, skipping", case_id, error_node_id)
        return []

    attrs = error_node.attrs
    band = attrs.get("severity_band", "low")
    if not meets_complication_trigger(band):
        logger.info("complication[%s]: %s is severity=%s, below trigger", case_id, error_node_id, band)
        await _record_outcome(case_id, error_node, "below_severity_threshold", f"severity {band} is below the reasoning threshold")
        return []

    phase_label = (index.snapshot_slot(node_ids.SNAPSHOT_CURRENT_PHASE) or {}).get("label", "")
    twin = index.snapshot_slot(node_ids.patient_twin()) or {}
    cache_key = (
        case_id,
        str(attrs.get("error_category")),
        phase_label,
        str((twin.get("attrs") or {}).get("profile", {}).get("profile_id")),
    )
    if cache_key in _seen:
        logger.info("complication[%s]: already reasoned for %s in this phase", case_id, attrs.get("error_category"))
        await _record_outcome(
            case_id, error_node, "already_reasoned",
            f"{attrs.get('error_category')} was already reasoned about during this phase",
        )
        return []
    _seen.add(cache_key)

    slice_context = complication_slice(index, error_node_id)

    # Step 1 — the agent composes its own question.
    query_out: LiteratureQuery = await run_llm_agent_once(
        QUERY_AGENT, _as_content(_format_slice(slice_context)), LiteratureQuery, app_name="surggraph_complication_query"
    )
    logger.info("complication[%s]: %d queries: %s", case_id, len(query_out.queries), query_out.queries)

    # Step 2 — real retrieval, several short queries in parallel, merged.
    hits, literature_node_ids, evidence_available = await retrieve(
        case_id, query_out.queries, parent_node_id=error_node_id
    )

    # Step 3 — reason over what actually came back.
    reasoning_prompt = _format_slice(slice_context, retrieved=hits)
    # Real MedGemma-vs-Gemini latency comparison, real user request — shadow
    # only, fired in the background, never awaited: it must never add to or
    # break this real reasoning step, only observe it. No-op if
    # MEDGEMMA_ENDPOINT_ID isn't set.
    fire_shadow_latency_call(f"complication_reasoning:{error_node_id}", reasoning_prompt)
    assessment: ComplicationAssessment = await run_llm_agent_once(
        REASONING_AGENT,
        _as_content(reasoning_prompt),
        ComplicationAssessment,
        app_name="surggraph_complication_reasoning",
    )

    if not assessment.candidates:
        logger.info("complication[%s]: no complications warranted for %s", case_id, error_node_id)
        await _record_outcome(case_id, error_node, "none_warranted", assessment.reasoning)
        return []

    patches: list[tuple] = []
    written: list[str] = []

    for candidate in assessment.candidates:
        node_id = node_ids.complication(error_node_id, candidate.name)
        written.append(node_id)
        patches.append(
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="complication",
                    label=candidate.name,
                    attrs={
                        "confidence": candidate.confidence,
                        "mechanism": candidate.mechanism,
                        "patient_specific_factor": candidate.patient_specific_factor,
                        # Carried explicitly so the verification gate can refuse
                        # an external write built on an ungrounded claim, and so
                        # the UI can show the difference honestly.
                        "evidence_backed": candidate.evidence_backed,
                        "evidence_available": evidence_available,
                        "queries_used": query_out.queries,
                        "root_error_id": error_node_id,
                        "reasoning": assessment.reasoning,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                candidate.mechanism,
            )
        )
        # error -> complication, the causal link (§4.2).
        patches.append(
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(error_node_id, node_id, "causal_reasoning"),
                    source_node_id=error_node_id,
                    target_node_id=node_id,
                    edge_kind="causal_reasoning",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason=candidate.mechanism,
                ),
                candidate.mechanism,
            )
        )
        # literature -> complication, but only from the paper the agent actually
        # cited. Linking every retrieved paper to every candidate would make the
        # evidence edges meaningless.
        idx = candidate.supporting_citation_index
        if candidate.evidence_backed and idx is not None and 0 <= idx < len(literature_node_ids):
            patches.extend(
                evidence_edges([literature_node_ids[idx]], node_id, f"Supports: {candidate.name}")
            )

    patches.append(
        (
            error_node.model_copy(
                update={
                    "attrs": {
                        **error_node.attrs,
                        "complication_status": "reasoned",
                        "complication_status_detail": f"{len(written)} complication(s) identified",
                    }
                }
            ),
            None,
            "Complication reasoning: reasoned",
        )
    )
    await apply_state_patches(case_id, patches)
    logger.info(
        "complication[%s]: %d candidate(s) from %s: %s",
        case_id,
        len(written),
        error_node_id,
        ", ".join(c.name for c in assessment.candidates),
    )
    return written


def subscribe(bus) -> None:
    """Registers this agent on a case's graph-change bus.

    The filter is the trigger: only error nodes, and only those at or above the
    severity band. Everything else on the bus is ignored without a call.
    """

    async def handler(event: StateDiffEvent) -> None:
        if event.node is None:
            return
        if not meets_complication_trigger(event.node.attrs.get("severity_band", "low")):
            return
        await reason_about_error(event.case_id, event.node.node_id)

    bus.subscribe(SOURCE_AGENT, handler, node_types={"error"})


async def ensure_agent_node(case_id: str) -> None:
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_ids.agent(SOURCE_AGENT),
                    node_type="agent",
                    label="Complication Reasoning",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "Complication Reasoning registered for this case",
            )
        ],
    )
