// Mirrors state/schema.py's Living Graph vocabulary (NodeType, EdgeKind) and
// its GraphNodePatch / GraphEdgePatch / StateDiffEvent shapes. Kept in sync by
// hand — the Python side is authoritative; see docs/plan_v2_autonomous_safety_system.md §4.

export type NodeType =
  // Structural — the case skeleton, drawn up front at case open
  | "trigger"
  | "agent"
  | "patient_twin"
  // Perception — the two-tier registry + event stream (plan_v2 §7)
  | "entity"
  | "perception_event"
  | "snapshot"
  | "phase"
  | "vitals"
  | "manual_event"
  // Reasoning chain
  | "error"
  | "complication"
  | "literature_evidence"
  | "corrective_trajectory"
  | "divergence_alert"
  // Action + safety
  | "action_intent"
  | "verification_block"
  | "action_outcome"
  // Post-case
  | "benchmark"
  | "documentation";

export type EdgeKind =
  | "detection"
  | "causal_reasoning"
  | "evidence"
  | "prediction"
  | "proposal"
  | "trajectory_comparison"
  | "confirmation"
  | "verification"
  | "grading"
  | "hierarchy"
  | "involved"
  | "outcome"
  | "succession";

export type ConfirmationSignal = "pending" | "confirmed" | "refuted";

export interface GraphNodePatch {
  node_id: string;
  node_type: NodeType;
  label: string;
  attrs: Record<string, unknown>;
  source_agent: string;
  source_tool: string;
  timestamp: string;
}

export interface GraphEdgePatch {
  edge_id: string;
  source_node_id: string;
  target_node_id: string;
  edge_kind: EdgeKind;
  trajectory_id?: string | null;
  confirmation_signal?: ConfirmationSignal | null;
  source_agent: string;
  source_tool: string;
  timestamp: string;
  reason?: string;
}

export type StateDiffOp = "add_node" | "update_node" | "add_edge" | "update_edge" | "remove_edge";

export interface StateDiffEvent {
  event_id: string;
  case_id: string;
  seq: number;
  timestamp: string;
  op: StateDiffOp;
  node?: GraphNodePatch;
  edge?: GraphEdgePatch;
  source_agent: string;
  source_tool: string;
  reason: string;
}

export interface CaseGraphNodeData extends Record<string, unknown> {
  nodeType: NodeType;
  label: string;
  sourceAgent: string;
  sourceTool: string;
  timestamp: string;
  confidence?: number;
  predicted?: boolean;
  confirmationSignal?: ConfirmationSignal;
  /** Error severity band — what actually gates downstream reasoning. */
  severityBand?: string;
  /** Real rendered width in px, measured and assigned by layoutGraph so the
   *  node renders at exactly the size dagre reserved for it (plan_v2 §4.1). */
  width?: number;
  raw: GraphNodePatch;
}

export interface CaseGraphEdgeData extends Record<string, unknown> {
  edgeKind: EdgeKind;
  sourceAgent: string;
  trajectoryId?: string;
  confirmationSignal?: ConfirmationSignal;
  reason?: string;
}

export interface StateSnapshot {
  case_id: string;
  seq: number;
  nodes: GraphNodePatch[];
  edges: GraphEdgePatch[];
}

// A running feed of "reason" strings, newest first — this is what makes the
// event-input/monitor tile (tile 4) and the retrieval tile (tile 5) read as
// autonomous reasoning happening live, not just a graph silently animating.
export interface ReasoningLogEntry {
  event_id: string;
  timestamp: string;
  source_agent: string;
  source_tool: string;
  reason: string;
}
