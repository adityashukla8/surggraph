// Mirrors state/schema.py's GraphNodePatch / GraphEdgePatch / StateDiffEvent.
// Keep these two files in sync by hand for now (small enough surface);
// revisit generating one from the other if the schema keeps changing.

export type NodeEntityType = "agent" | "phase" | "entity" | "artifact" | "event";
export type ConfirmationSignal = "pending" | "confirmed" | "refuted";
export type EdgeKind = "predicted" | "action" | "observed" | "revised";

export interface GraphNodePatch {
  node_id: string;
  node_type: NodeEntityType;
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
  entityType: NodeEntityType;
  label: string;
  sourceAgent: string;
  sourceTool: string;
  timestamp: string;
  confidence?: number;
  predicted?: boolean;
  confirmationSignal?: ConfirmationSignal;
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
