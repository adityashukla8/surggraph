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

from agents.documentation.subagent import _INSTRUCTION, OperativeNoteDraft
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch
from tools.context_slice import GraphIndex, documentation as documentation_slice
from tools.gemini_model import GEMINI_MODEL, generate_structured
from tools.model_armor import join_note_sections, screen_operative_note
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "documentation"
_SOURCE_TOOL = "draft_operative_note"


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

    node_id = node_ids.documentation(case_id)

    # A real status write, not a fake placeholder: drafting is genuinely
    # underway at this point (the Gemini call below is what's slow), so the
    # panel has something honest to show for the ~1-2 minutes this takes
    # instead of going silent. The final write below lands on this same
    # node_id and overwrites it once the real draft exists.
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="documentation",
                    label="Preparing operative report…",
                    attrs={"approval_status": "drafting"},
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "Drafting the operative record",
            ),
        ],
    )

    prompt = _format_case(slice_context, index)
    # Gemini 3.5 writes the operative record — the same model every other
    # agent in this system reasons with. (Was MedGemma, a self-deployed
    # medical-domain model, undeployed after repeated generation failures
    # on certain cases regardless of machine shape, GPU count, or token
    # ceiling — see docs/qa_log.md.)
    draft = await generate_structured(
        f"documentation:{case_id}",
        _INSTRUCTION,
        prompt,
        OperativeNoteDraft,
    )
    sections = draft.model_dump()

    # Screened HERE, autonomously, before a surgeon ever sees an Approve
    # button — not just at approval time. This is the same fail-closed gate
    # agents/hitl/approval.py::_file_record_impl re-runs at approval time
    # (re-screening whatever the surgeon actually edited, which is the one
    # thing that can genuinely change between now and then); this pass is
    # what gives the surgeon advance visibility instead of only finding out
    # after clicking Approve.
    model_armor_node_id = node_ids.model_armor_screen(node_id)
    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=model_armor_node_id,
                    node_type="model_armor_screen",
                    label="Model Armor screening operative note…",
                    attrs={"status": "screening"},
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                "Screening the freshly drafted operative note for injected or sensitive content",
            ),
            (
                None,
                GraphEdgePatch(
                    edge_id=node_ids.edge(node_id, model_armor_node_id, "verification"),
                    source_node_id=node_id,
                    target_node_id=model_armor_node_id,
                    edge_kind="verification",
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                    reason="Content-safety screening of the freshly drafted note",
                ),
                "Content-safety screening of the freshly drafted note",
            ),
        ],
    )

    screen = screen_operative_note(join_note_sections(sections))

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=model_armor_node_id,
                    node_type="model_armor_screen",
                    label=(f"BLOCKED — {screen.reason}" if screen.blocked else "Passed — cleared for review"),
                    attrs={
                        "status": "blocked" if screen.blocked else "passed",
                        "reason": screen.reason,
                        "raw_filter_match_state": screen.raw_filter_match_state,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                screen.reason or "Model Armor cleared this draft for surgeon review",
            ),
        ],
    )

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="documentation",
                    label=(
                        "Operative record draft — blocked by Model Armor"
                        if screen.blocked
                        else "Operative record draft — awaiting surgeon approval"
                    ),
                    attrs={
                        # A blocked draft still needs surgeon eyes on WHY, so
                        # sections are kept rather than withheld — only the
                        # approval action itself is unavailable (see
                        # ui/frontend's AutonomousActionsPanel, which hides
                        # Approve for this status and offers only Reject).
                        "approval_status": "blocked" if screen.blocked else "pending",
                        "sections": sections,
                        "model_armor_reason": screen.reason,
                        # Real provenance, checkable in the graph.
                        "drafted_by_model": GEMINI_MODEL,
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

    logger.info(
        "documentation[%s]: drafted, %d limitation(s) stated, model armor %s",
        case_id,
        len(draft.limitations),
        "BLOCKED" if screen.blocked else "passed",
    )
    return node_id
