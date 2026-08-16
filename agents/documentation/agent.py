"""Documentation Agent — drafts the operative record at case close.

docs/agentic_workflow.md §5 agent 11, docs/plan_v2 §6 step 12. Runs after
Benchmark, so the draft can tell a reader how much to trust it.

This is the project's second value axis: the reasoning graph already contains
the operative narrative, so the record is a byproduct of reasoning that already
happened rather than a separate authoring pass.

A REAL REASONING CALL, unlike Benchmark and the verification gate. Synthesising
a readable clinical narrative from a whole case graph is exactly what a language
model is for, and there is no deterministic way to do it that would not be a
template with the numbers swapped in.

WHAT REACHES THE MODEL IS FILTERED, NOT SUMMARISED. The documentation context
slice carries the whole case, but the whole case includes thirty-plus
perception events like "needle driver entered the field" that would bury the
clinically meaningful content. So the prompt gets the reasoning chain in full
and perception compressed to its activity progression — the narrative — rather
than every observation.

NOTHING IS WRITTEN EXTERNALLY HERE. The draft lands on the graph with
`approval_status: pending`. A surgeon approves it (HITL #2), it passes the
verification gate, and only then is it filed.
"""

from __future__ import annotations

import logging

from google.genai import types

from agents.documentation.subagent import OperativeNoteDraft, build_subagent
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.adk_runner import run_llm_agent_once
from tools.context_slice import GraphIndex, documentation as documentation_slice
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "documentation"
_SOURCE_TOOL = "draft_operative_note"

AGENT = build_subagent()


def _format_case(slice_context: dict, index: GraphIndex) -> str:
    """Renders the case for the drafting prompt.

    Reasoning content goes in whole; perception goes in as its activity
    progression only. A case produces dozens of entity-appeared events, and
    including them would crowd out the complications and proposals that the
    document is actually about.
    """
    lines: list[str] = []

    twin = slice_context.get("patient_twin") or {}
    lines += [
        "PATIENT (synthetic, illustrative — not a real record)",
        f"  {(twin.get('attrs') or {}).get('prompt_summary', '(none loaded)')}",
        "",
        "OBSERVED ACTIVITY PROGRESSION (in order)",
    ]
    phases = slice_context.get("phases") or []
    if not phases:
        lines.append("  (none observed)")
    for p in phases:
        lines.append(f"  - {p['label']}")

    lines += ["", "TECHNIQUE ERRORS FLAGGED BY AUTOMATED DETECTION"]
    errors = slice_context.get("errors") or []
    if not errors:
        lines.append("  (none flagged)")
    for e in errors:
        a = e.get("attrs", {})
        lines.append(
            f"  - {e['label']} | category {a.get('error_category')} | severity {a.get('severity_band')} "
            f"| detector confidence {a.get('confidence')}"
        )
        if a.get("reasoning"):
            lines.append(f"      detector's stated evidence: {a['reasoning']}")
        if a.get("complication_status") and a["complication_status"] != "reasoned":
            lines.append(f"      downstream reasoning: {a['complication_status']} — {a.get('complication_status_detail','')}")

    lines += ["", "COMPLICATIONS THE SYSTEM REASONED ABOUT"]
    complications = slice_context.get("complications") or []
    if not complications:
        lines.append("  (none reasoned)")
    for c in complications:
        a = c.get("attrs", {})
        grounded = "LITERATURE-GROUNDED" if a.get("evidence_backed") else "NOT evidence-supported"
        lines.append(f"  - {c['label']} | confidence {a.get('confidence')} | {grounded}")
        lines.append(f"      proposed mechanism: {a.get('mechanism','')}")
        if a.get("patient_specific_factor"):
            lines.append(f"      patient factor: {a['patient_specific_factor']}")
        # Only papers that genuinely support it — the evidence edges.
        for lit in index.neighbors_in(c["id"], edge_kind="evidence"):
            lines.append(f"      supporting paper: {lit.label} ({lit.attrs.get('journal')} {lit.attrs.get('year')})")

    lines += ["", "CORRECTIVE PLANS PROPOSED, AND THE SURGEON'S RESPONSE"]
    proposals = slice_context.get("corrective_proposals") or []
    if not proposals:
        lines.append("  (none proposed)")
    for p in proposals:
        a = p.get("attrs", {})
        if a.get("escalated"):
            lines.append(f"  - ESCALATED, no plan offered: {a.get('escalation_reason','')}")
            continue
        response = a.get("acknowledgment_outcome") or "no response recorded"
        lines.append(f"  - {p['label']} | urgency {a.get('urgency')} | surgeon: {response}")
        for step in a.get("steps", []):
            lines.append(f"      step {step['order']}: {step['action']}")
        prov = a.get("provenance") or {}
        lines.append(f"      action provenance: tier {prov.get('tier')} — {prov.get('review_status','')}")

    lines += ["", "DIVERGENCE ALERTS"]
    alerts = slice_context.get("divergence_alerts") or []
    if not alerts:
        lines.append("  (none — no proposal was measurably departed from)")
    for al in alerts:
        a = al.get("attrs", {})
        lines.append(
            f"  - {a.get('reasoning','')} | detected {a.get('detection_method')} "
            f"| {'advisory only (surgeon had acknowledged)' if a.get('advisory') else 'not acknowledged'}"
        )

    lines += ["", "EXTERNAL ALERTS ATTEMPTED"]
    outcomes = slice_context.get("action_outcomes") or []
    blocks = slice_context.get("verification_blocks") or []
    if not outcomes and not blocks:
        lines.append("  (none)")
    for b in blocks:
        a = b.get("attrs", {})
        lines.append(f"  - verification {'PASSED' if a.get('passed') else 'BLOCKED'}: {b['label']}")
    for o in outcomes:
        lines.append(f"  - {o['label']}: {(o.get('attrs') or {}).get('detail','')}")

    lines += ["", "PHYSIOLOGICAL DEVIATIONS"]
    vitals = [n for n in index.of_type("vitals")]
    if not vitals:
        lines.append("  (none flagged)")
    for v in sorted(vitals, key=lambda n: n.timestamp):
        a = v.attrs
        lines.append(f"  - at {a.get('t_s')}s: {a.get('excursion_label') or 'deviation'} "
                     f"(HR {a.get('hr_bpm')}, MAP {a.get('map_mmhg')}, SpO2 {a.get('spo2_pct')})")

    lines += ["", "THIS CASE'S OWN BENCHMARK AGAINST GROUND TRUTH"]
    benchmarks = slice_context.get("benchmark") or []
    if not benchmarks:
        lines.append("  (not scored — no ground truth available for this video)")
    for b in benchmarks:
        a = b.get("attrs", {})
        lines.append(
            f"  - {a.get('n')} windows scored on the {a.get('axis')} axis: "
            f"tp={a.get('tp')} fp={a.get('fp')} fn={a.get('fn')} tn={a.get('tn')}"
        )
        lines.append(f"      macro-F1 {a.get('macro_f1')} against CARES' published {a.get('cares_published_macro_f1')}")
        lines.append(f"      {a.get('axis_note','')}")

    return "\n".join(lines)


async def draft_note(case_id: str) -> str | None:
    """Drafts the operative record. Returns the documentation node id."""
    index = GraphIndex(await get_state_snapshot(case_id))
    slice_context = documentation_slice(index)

    if not index.of_type("phase") and not index.of_type("error"):
        logger.warning("documentation[%s]: nothing observed in this case, not drafting", case_id)
        return None

    prompt = _format_case(slice_context, index)
    draft: OperativeNoteDraft = await run_llm_agent_once(
        AGENT,
        types.Content(role="user", parts=[types.Part(text=prompt)]),
        OperativeNoteDraft,
        app_name="surggraph_documentation",
    )

    node_id = node_ids.documentation(case_id)
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="documentation",
                    label="Operative record draft — awaiting surgeon approval",
                    attrs={
                        "approval_status": "pending",
                        "sections": draft.model_dump(),
                        # Carried so nothing downstream can present the draft as
                        # more settled than it is.
                        "synthetic_patient": True,
                        "surgeon_reviewed": False,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                draft.summary,
            ),
            # Traced to the benchmark it reports, when there is one.
            *(
                [
                    (
                        None,
                        GraphEdgePatch(
                            edge_id=node_ids.edge(node_ids.benchmark(case_id), node_id, "hierarchy"),
                            source_node_id=node_ids.benchmark(case_id),
                            target_node_id=node_id,
                            edge_kind="hierarchy",
                            source_agent=SOURCE_AGENT,
                            source_tool=_SOURCE_TOOL,
                            reason="The draft reports this benchmark",
                        ),
                        "The draft reports this benchmark",
                    )
                ]
                if index.nodes_by_id.get(node_ids.benchmark(case_id))
                else []
            ),
        ],
    )

    logger.info("documentation[%s]: drafted, %d limitation(s) stated", case_id, len(draft.limitations))
    return node_id
