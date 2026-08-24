"""The Living Surgical State schema.

Single source of truth for every shape that crosses an agent boundary.
Agents never carry case state in their prompt context — they read/write
through services/state_service, which persists and streams these models
(see plan §4.2, initial_11082026.md §5.3/§9 "state is queried, not carried").

ui/frontend/src/graph/types.ts mirrors the wire-facing subset of this file
(GraphNodePatch, GraphEdgePatch, StateDiffEvent) by hand — keep both in sync
when either changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Provenance — every node, edge, and action carries this (context doc §9).
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Provenanced(BaseModel):
    source_agent: str
    source_tool: str
    timestamp: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# The Living Graph vocabulary — docs/plan_v2_autonomous_safety_system.md §4.
#
# The graph is the system's ground truth for what has happened, what is
# happening, what is predicted to happen, and what is proposed to happen.
# Every reasoning step reads from it and writes to it, and the storage layer
# below it knows nothing about surgery — only generic multi-tenant graph
# storage and change streaming.
#
# ui/frontend/src/graph/types.ts mirrors this by hand — keep both in sync.
# ---------------------------------------------------------------------------

NodeType = Literal[
    # --- Structural: the case skeleton, drawn up front at case open ---------
    "trigger",  # the case-open event the whole pipeline hangs off
    "agent",  # agent:{name} — a reasoning role in the hierarchy
    "patient_twin",  # the synthetic patient profile, loaded once per case
    # --- Perception (plan_v2 §7): the two-tier registry + event stream ------
    "entity",  # entity:{stable_id} — long-lived, updated in place
    "perception_event",  # event:{seq}:{kind} — append-only, immutable
    "snapshot",  # snapshot:{slot} — singleton, fixed cardinality
    "phase",  # phase:{opaque_id}:{window} — current + historical + predicted
    "vitals",  # a physiological-state node, written on trend flag/excursion
    "manual_event",  # a human-typed event, honestly tagged as such
    # --- Reasoning chain ----------------------------------------------------
    "error",  # error:{window}:{category} — one per detected event
    "complication",  # complication:{root_error_id}:{slug}
    "literature_evidence",  # literature:{query_hash}:{index}
    "corrective_trajectory",  # corrective:{root_error_id}:{slug}
    "divergence_alert",  # divergence:{proposal_id}:{window}
    # --- Action + safety ----------------------------------------------------
    "action_intent",  # action_intent:{kind}:{ulid} — a pending external write
    "verification_block",  # verification:{action_intent_id} — fail-closed gate outcome
    "model_armor_screen",  # model_armor:{action_intent_id} — second fail-closed gate, content safety
    "action_outcome",  # the external write's real delivery result
    # --- Post-case ----------------------------------------------------------
    "benchmark",  # benchmark:{case_id} — singleton, predicted-vs-actual scorecard
    "documentation",  # documentation:{case_id} — singleton, operative note draft
]

EdgeKind = Literal[
    # --- The nine kinds named in plan_v2 §4.2 -------------------------------
    "detection",  # perception -> error/divergence
    "causal_reasoning",  # error -> complication
    "evidence",  # literature -> complication or corrective_trajectory
    "prediction",  # current state -> future trajectory state (dashed)
    "proposal",  # error+complication -> corrective_trajectory (dashed)
    "trajectory_comparison",  # actual -> corrective, carries alignment/divergence signal
    "confirmation",  # predicted node reconciled against realized state
    "verification",  # proposed action -> verification_block outcome
    "grading",  # predicted -> ground truth, post-case only
    # --- Four more the workflow prose requires but §4.2's table omits -------
    "hierarchy",  # orchestrator -> agent -> what that agent produced (§6 step 1's up-front draw)
    "involved",  # perception_event -> entity (§7.3: events reference entities, never copy them)
    "outcome",  # action_intent -> action_outcome (§6 steps 10/14)
    # §4.3 requires every node be tied to and orderable by timestamp. As a
    # graph relation rather than only a sort key, that is this edge: "this
    # followed that". It is what turns a pile of timestamped nodes into a
    # readable chronological spine — the activity at 0s leading to the
    # activity at 15s leading to the next — instead of a scatter the viewer
    # has to reassemble mentally.
    "succession",
]

ConfirmationSignal = Literal["pending", "confirmed", "refuted"]

# Legacy alias. The old vocabulary was agent|phase|entity|artifact|event;
# "artifact" split into action_intent/action_outcome and "event" split into
# perception_event/error/divergence_alert/manual_event, because a
# high-frequency "entity appeared" must never render like an alarm.
NodeEntityType = NodeType


class GraphNodePatch(Provenanced):
    node_id: str
    node_type: NodeEntityType
    label: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphEdgePatch(Provenanced):
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_kind: EdgeKind
    trajectory_id: str | None = None
    confirmation_signal: ConfirmationSignal | None = None
    reason: str = ""


class StateDelta(BaseModel):
    add_nodes: list[GraphNodePatch] = Field(default_factory=list)
    add_edges: list[GraphEdgePatch] = Field(default_factory=list)
    remove_edge_ids: list[str] = Field(default_factory=list)


StateDiffOp = Literal["add_node", "update_node", "add_edge", "update_edge", "remove_edge"]


class StateDiffEvent(Provenanced):
    """The unit the state service streams over SSE. `seq` is monotonic per
    case_id — the frontend uses gaps in it to detect a missed event and
    resync from a fresh snapshot rather than silently render an incomplete
    graph (plan §4.2)."""

    event_id: str = Field(default_factory=lambda: _new_id("evt"))
    case_id: str
    seq: int
    op: StateDiffOp
    node: GraphNodePatch | None = None
    edge: GraphEdgePatch | None = None
    reason: str


class StateSnapshot(BaseModel):
    case_id: str
    seq: int
    nodes: list[GraphNodePatch]
    edges: list[GraphEdgePatch]


# ---------------------------------------------------------------------------
# Trajectory patches — candidate plans, anticipated complications, and
# recovery options all share this shape (plan §3.4).
# ---------------------------------------------------------------------------


class RiskVector(BaseModel):
    likelihood: float = Field(ge=0, le=1)
    severity: float = Field(ge=0, le=1)
    composite: float = Field(ge=0, le=1)
    reasoning: str  # Gemini-estimated from retrieved evidence, never formula-weighted


class EvidenceCitation(BaseModel):
    doc_id: str
    pmcid: str | None = None
    title: str
    url: str
    snippet: str
    relevance_score: float = Field(ge=0, le=1)
    retrieved_by: Literal["complication_enumeration_agent", "literature_agent"]
    query_used: str  # the query Gemini itself formulated — never a static template
    retrieved_live: bool
    timestamp: datetime = Field(default_factory=_now)


TrajectoryKind = Literal["candidate_plan", "anticipated_complication", "recovery_option"]


class TrajectoryPatch(Provenanced):
    trajectory_id: str = Field(default_factory=lambda: _new_id("traj"))
    kind: TrajectoryKind
    trigger_phase: str
    anticipated: bool
    description: str
    predicted_state_delta: StateDelta
    risk_vector: RiskVector
    evidence: list[EvidenceCitation] = Field(default_factory=list)
    suggested_precautions: list[str] = Field(default_factory=list)
    confirmation_signal: ConfirmationSignal = "pending"
    confirmed_by: str | None = None  # a DivergenceEvent.event_id, once confirmed
    parent_trajectory_id: str | None = None  # set on recovery_option patches


# ---------------------------------------------------------------------------
# Anticipation Agent output (plan §2.4).
# ---------------------------------------------------------------------------


class AnticipationResult(Provenanced):
    case_id: str
    at_frame: int
    current_phase: str
    next_phase: str
    confidence: float = Field(ge=0, le=1)
    eta_seconds: float
    reasoning: str
    prior_top_candidate: str
    prior_top_prob: float
    coverage_n: int  # sample size behind the prior — low values are real, not fabricated, but thin
    deviated_from_prior: bool


# ---------------------------------------------------------------------------
# Divergence — a real, live multi-agent detection (CARES-style; plan §3.5),
# never a SEDMamba ground-truth lookup. SEDMamba's real labels are used only
# by the validation sidecar (tools/sedmamba_labels.py) to score detections
# after the fact — never to decide them. See agents/error_detection/ for the
# detection pipeline this model is the output of.
# ---------------------------------------------------------------------------

ErrorCategory = Literal[
    "multiple_attempts",
    "out_of_view",
    "needle_handling",
    "tissue_handling",
    "suture_handling",
    "instrument_control",
]
SubAgentRole = Literal["temporal", "spatial", "procedural"]
ExpertiseTier = Literal["resident", "attending", "expert"]


class ErrorDetectionSubAgentAssessment(BaseModel):
    agent_role: SubAgentRole
    tier_used: ExpertiseTier
    error_present: bool  # O_{p,x}(V) in CARES' Eq. 7 notation
    confidence: float = Field(ge=0, le=1)
    reasoning: str
    frames_examined: list[int] = Field(default_factory=list)  # real video frame numbers sent to this call


class DivergenceEvent(Provenanced):
    event_id: str = Field(default_factory=lambda: _new_id("div"))
    case_id: str
    window_id: str
    frame: int  # window_start_frame, representative (kept for compatibility with callers reading .frame)
    window_start_frame: int
    window_end_frame: int
    phase: str
    error_category: ErrorCategory | Literal["manual_injection"]
    source: Literal["error_detection_agentic", "manual_injection"]
    sub_agent_assessments: list[ErrorDetectionSubAgentAssessment] = Field(default_factory=list)  # len 3 for agentic; [] for manual
    psi: int | None = None  # Ψ = TIS + CIS, set only if pass-2 risk-routing ran for this window
    tier_used: ExpertiseTier | None = None
    composite_score: float | None = None  # CARES Eq. 7 weighted sum
    threshold_used: float | None = None
    confidence: float = Field(ge=0, le=1)
    reasoning_trace: str  # the coordinator's synthesis across sub-agents, not any one agent's text alone
    confirms_trajectory_id: str | None = None  # sets the matched TrajectoryPatch.confirmed_by, if any
    raw_label: dict[str, Any] = Field(default_factory=dict)  # VALIDATION-ONLY: real SEDMamba ground truth for
    # this window, populated post-hoc by the validation sidecar — never read before the detection is decided


# ---------------------------------------------------------------------------
# Verifier — fail-closed blocks must be visible, structured events (plan §7,
# non-negotiable #2).
# ---------------------------------------------------------------------------


class VerifierBlockEvent(Provenanced):
    event_id: str = Field(default_factory=lambda: _new_id("block"))
    case_id: str
    claim_id: str
    reason: str
    blocked_payload_summary: str


# ---------------------------------------------------------------------------
# Action Router — the four-way autonomous routing decision (context doc
# §5.2 item 11, plan §7).
# ---------------------------------------------------------------------------

RouteDecision = Literal["write_only", "write_and_alert", "no_action", "human_escalate"]


class RouterDecisionRecord(Provenanced):
    event_id: str = Field(default_factory=lambda: _new_id("route"))
    case_id: str
    decision: RouteDecision
    reasoning: str
    verifier_passed: bool
    fhir_write_resource_id: str | None = None
    alert_fired: bool = False


# ---------------------------------------------------------------------------
# CaseState — the full case document persisted via Memory Bank/Firestore
# (context doc §5.3). Nested per-section models below; contingency_branches
# and predicted_risks are explicitly TrajectoryPatch lists per plan §3.4.
# ---------------------------------------------------------------------------


class PatientRecord(BaseModel):
    synthetic_record_ref: str
    mri_series_ref: str
    risk_flags: list[str] = Field(default_factory=list)


class ProcedureState(BaseModel):
    phase: str
    step: str
    completed_actions: list[str] = Field(default_factory=list)
    expected_next: list[str] = Field(default_factory=list)


class Entities(BaseModel):
    instruments: list[str] = Field(default_factory=list)
    anatomy: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)


class Relation(BaseModel):
    instrument: str
    verb: str
    target: str


class Relations(BaseModel):
    spatial: list[Relation] = Field(default_factory=list)
    action: list[Relation] = Field(default_factory=list)
    interaction: list[Relation] = Field(default_factory=list)


class Mission(BaseModel):
    goal: str
    active_plan: TrajectoryPatch | None = None
    rejected_alternatives: list[TrajectoryPatch] = Field(default_factory=list)
    contingency_branches: list[TrajectoryPatch] = Field(default_factory=list)


class Predictions(BaseModel):
    rehearsed_futures: list[TrajectoryPatch] = Field(default_factory=list)
    predicted_risks: list[TrajectoryPatch] = Field(default_factory=list)
    phase_forecast: AnticipationResult | None = None


class Uncertainty(BaseModel):
    anatomy_conf: float = Field(ge=0, le=1, default=1.0)
    state_conf: float = Field(ge=0, le=1, default=1.0)
    risk_conf: float = Field(ge=0, le=1, default=1.0)


class Evidence(BaseModel):
    video_refs: list[str] = Field(default_factory=list)
    mri_refs: list[str] = Field(default_factory=list)
    literature_refs: list[EvidenceCitation] = Field(default_factory=list)
    guideline_refs: list[str] = Field(default_factory=list)  # cited by name/link only, never ingested


class History(BaseModel):
    deviations: list[DivergenceEvent] = Field(default_factory=list)
    recoveries: list[TrajectoryPatch] = Field(default_factory=list)
    agent_actions: list[RouterDecisionRecord] = Field(default_factory=list)
    verifier_blocks: list[VerifierBlockEvent] = Field(default_factory=list)


class CaseState(BaseModel):
    case_id: str
    timestamp: datetime = Field(default_factory=_now)
    procedure: Literal["RARP"] = "RARP"

    patient: PatientRecord
    procedure_state: ProcedureState
    entities: Entities = Field(default_factory=Entities)
    relations: Relations = Field(default_factory=Relations)
    mission: Mission
    predictions: Predictions = Field(default_factory=Predictions)
    uncertainty: Uncertainty = Field(default_factory=Uncertainty)
    evidence: Evidence = Field(default_factory=Evidence)
    history: History = Field(default_factory=History)

    seq: int = 0  # bumped on every applied StateDiffEvent; mirrors StateSnapshot.seq
