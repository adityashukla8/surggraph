"""Corrective Replanning Agent — event-driven off complication nodes.

docs/agentic_workflow.md §3 agent 6, docs/plan_v2 §6 step 5.

    complication node
        -> assemble the full replanning slice (triggering error, complication
           candidates with their literature, patient twin, vitals, current and
           recent phases, any proposal already active)
        -> the agent SELECTS from the bounded action library for that error's
           category, or escalates
        -> corrective_trajectory node, dotted-outline, linked by proposal edges
           back to both the complication and the root error

This is the first step in the system whose output is a suggestion about what a
surgeon should do, rather than a description of what happened or a hypothesis
about what might. Three things keep that honest:

  BOUNDED VOCABULARY  the agent picks action_ids from a reviewed list and the
                      loader rejects anything outside it, so the worst case is
                      the wrong action from that list, never an invented one.
  PROVENANCE TRAVELS  every proposal node carries the library's tier-2 sourcing
                      and its explicit "not reviewed by a practising surgeon"
                      status, so nothing downstream can present it as more than
                      it is.
  ESCALATION IS REAL  "nothing here fits" produces a visible node and no
                      proposal, rather than a forced weak match — which matters
                      because a proposal is what divergence detection later
                      measures the surgeon's actual moves against.
"""

from __future__ import annotations

import logging

from google.genai import types

from agents.corrective_replanning import library
from agents.corrective_replanning.subagent import CorrectiveProposal, build_subagent
from state import node_ids
from state.schema import GraphEdgePatch, GraphNodePatch, StateDiffEvent
from tools.adk_runner import run_llm_agent_once
from tools.context_slice import GraphIndex, corrective_replanning as replanning_slice
from tools.state_tools import apply_state_patches, get_state_snapshot

logger = logging.getLogger(__name__)

SOURCE_AGENT = "corrective_replanning"
_SOURCE_TOOL = "propose_corrective_trajectory"

AGENT = build_subagent()

# One proposal per complication. docs §9's idempotency key — re-reasoning the
# same complication produces the same proposal and costs a Gemini call to
# rediscover what is already on the graph.
_seen: set[tuple[str, str]] = set()


def _format_slice(slice_context: dict, category: str) -> str:
    complication = slice_context.get("triggering_complication") or {}
    errors = slice_context.get("root_errors") or []
    literature = slice_context.get("supporting_literature") or []
    twin = slice_context.get("patient_twin") or {}
    vitals = slice_context.get("vitals_trend") or {}
    phase = slice_context.get("current_phase") or {}
    active = slice_context.get("active_proposals") or []

    lines = [
        "TRIGGERING COMPLICATION",
        f"  {complication.get('label', '(unknown)')} "
        f"(confidence {complication.get('attrs', {}).get('confidence')}, "
        f"literature-grounded: {complication.get('attrs', {}).get('evidence_backed')})",
        f"  mechanism: {complication.get('attrs', {}).get('mechanism', '')}",
        f"  patient factor: {complication.get('attrs', {}).get('patient_specific_factor', '')}",
        "",
        "ROOT ERROR(S)",
    ]
    for e in errors:
        a = e.get("attrs", {})
        lines.append(f"  {e['label']} — category {a.get('error_category')}, severity {a.get('severity_band')}")
        lines.append(f"    detector's reasoning: {a.get('reasoning', '')}")

    if literature:
        lines += ["", "SUPPORTING LITERATURE"]
        for lit in literature:
            lines.append(f"  - {lit['label']}")

    lines += [
        "",
        "PATIENT (synthetic, illustrative)",
        f"  {twin.get('attrs', {}).get('prompt_summary', '(no profile loaded)')}",
        "",
        "VITALS",
        f"  {vitals.get('attrs', {}).get('excursion_label') or 'within expected range for this point in the case'}",
        "",
        "CURRENT ACTIVITY",
        f"  {phase.get('label', '(unknown)')}",
    ]

    if active:
        lines += ["", "ALREADY-ACTIVE PROPOSALS (do not restate these)"]
        lines += [f"  - {p['label']}" for p in active]

    lines += [
        "",
        f"AVAILABLE CORRECTIVE ACTIONS for category '{category}' — SELECT BY action_id ONLY:",
        library.format_for_prompt(category),
    ]
    return "\n".join(lines)


async def propose_for_complication(case_id: str, complication_node_id: str) -> list[str]:
    """Runs replanning for one complication. Returns node ids written."""
    if (case_id, complication_node_id) in _seen:
        return []
    _seen.add((case_id, complication_node_id))

    index = GraphIndex(await get_state_snapshot(case_id))
    complication = index.nodes_by_id.get(complication_node_id)
    if complication is None:
        logger.warning("corrective[%s]: %s not on the graph", case_id, complication_node_id)
        return []

    slice_context = replanning_slice(index, complication_node_id)

    root_errors = slice_context.get("root_errors") or []
    if not root_errors:
        logger.warning("corrective[%s]: %s has no root error, cannot pick a library", case_id, complication_node_id)
        return []
    root_error = root_errors[0]
    root_error_id = root_error["id"]
    category = root_error.get("attrs", {}).get("error_category")
    if not category:
        logger.warning("corrective[%s]: root error %s has no category", case_id, root_error_id)
        return []

    prompt = _format_slice(slice_context, category)
    proposal: CorrectiveProposal = await run_llm_agent_once(
        AGENT,
        types.Content(role="user", parts=[types.Part(text=prompt)]),
        CorrectiveProposal,
        app_name="surggraph_corrective_replanning",
    )

    # Enforcement, not trust: anything outside the library is dropped here even
    # though the prompt asked the model to stay inside it.
    resolved, rejected = library.resolve(category, [a.action_id for a in sorted(proposal.actions, key=lambda x: x.order)])
    why_by_id = {a.action_id: a.why_this_action for a in proposal.actions}

    if proposal.escalate or not resolved:
        reason = proposal.escalation_reason or "no library action confidently matched this situation"
        node_id = node_ids.corrective_trajectory(root_error_id, "escalate")
        await apply_state_patches(
            case_id,
            [
                (
                    GraphNodePatch(
                        node_id=node_id,
                        node_type="corrective_trajectory",
                        label=f"Escalate — no confident corrective match",
                        attrs={
                            "escalated": True,
                            "escalation_reason": reason,
                            "rejected_action_ids": rejected,
                            "root_error_id": root_error_id,
                            "provenance": library.provenance(),
                        },
                        source_agent=SOURCE_AGENT,
                        source_tool=_SOURCE_TOOL,
                    ),
                    None,
                    reason,
                ),
                _proposal_edge(complication_node_id, node_id, reason),
                _proposal_edge(root_error_id, node_id, reason),
            ],
        )
        logger.info("corrective[%s]: escalated for %s — %s", case_id, complication_node_id, reason)
        return [node_id]

    node_id = node_ids.corrective_trajectory(root_error_id, proposal.summary or category)
    steps = [
        {
            "order": i + 1,
            "action_id": a.action_id,
            "action": a.action,
            "rationale": a.rationale,
            # What divergence detection later compares the surgeon's actual
            # moves against — a proposal with no checkable outcome cannot be
            # diverged from in any measurable way.
            "verification_check": a.verification_check,
            "why_this_action": why_by_id.get(a.action_id, ""),
        }
        for i, a in enumerate(resolved)
    ]

    await apply_state_patches(
        case_id,
        [
            (
                GraphNodePatch(
                    node_id=node_id,
                    node_type="corrective_trajectory",
                    label=proposal.summary or f"Corrective plan for {category.replace('_', ' ')}",
                    attrs={
                        "escalated": False,
                        "urgency": proposal.urgency,
                        "steps": steps,
                        "root_error_id": root_error_id,
                        "complication_id": complication_node_id,
                        "rejected_action_ids": rejected,
                        # Sourcing travels with the proposal, not just with the
                        # library file, so nothing downstream can present this
                        # as stronger than tier-2 unreviewed content.
                        "provenance": library.provenance(),
                        # HITL #1 fills these in; absent means not yet seen.
                        "acknowledgment_outcome": None,
                    },
                    source_agent=SOURCE_AGENT,
                    source_tool=_SOURCE_TOOL,
                ),
                None,
                proposal.summary,
            ),
            _proposal_edge(complication_node_id, node_id, proposal.summary),
            _proposal_edge(root_error_id, node_id, proposal.summary),
        ],
    )
    logger.info(
        "corrective[%s]: proposed %d step(s) for %s: %s",
        case_id,
        len(steps),
        complication_node_id,
        ", ".join(s["action_id"] for s in steps),
    )
    return [node_id]


def _proposal_edge(source_node_id: str, target_node_id: str, reason: str) -> tuple:
    """§4.2: error+complication -> corrective_trajectory, dashed. Both endpoints
    get an edge so the proposal traces back to the full reasoning that produced
    it, not just the last link."""
    return (
        None,
        GraphEdgePatch(
            edge_id=node_ids.edge(source_node_id, target_node_id, "proposal"),
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_kind="proposal",
            source_agent=SOURCE_AGENT,
            source_tool=_SOURCE_TOOL,
            reason=reason,
        ),
        reason,
    )


def subscribe(bus) -> None:
    async def handler(event: StateDiffEvent) -> None:
        if event.node is None:
            return
        await propose_for_complication(event.case_id, event.node.node_id)

    bus.subscribe(SOURCE_AGENT, handler, node_types={"complication"})
