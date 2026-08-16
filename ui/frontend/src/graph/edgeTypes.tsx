import { BaseEdge, getBezierPath, type EdgeProps, type Edge } from "@xyflow/react";
import type { CaseGraphEdgeData } from "./types";
import { agentColorVar, DASHED_EDGE_KINDS, EDGE_KIND_COLOR, CONFIRMATION_STATUS_COLOR } from "./palette";

export type CaseFlowEdge = Edge<CaseGraphEdgeData, "caseEdge">;

// Weight encodes how load-bearing an edge is, independent of its color.
// The two structural kinds that exist in bulk (the case skeleton, and every
// perception event's link to the entities it involves) are drawn thin and
// quiet so they never compete with the reasoning chain for attention.
const STRUCTURAL_KINDS = new Set<CaseGraphEdgeData["edgeKind"]>(["hierarchy", "involved", "succession"]);

// A confirmed prediction and a fail-closed gate outcome are the two edges a
// viewer most needs to catch, so they get the heaviest stroke.
const EMPHASIZED_KINDS = new Set<CaseGraphEdgeData["edgeKind"]>(["confirmation", "verification"]);

function edgeStyle(data: CaseGraphEdgeData | undefined): React.CSSProperties {
  if (!data) return {};

  // A refuted prediction is greyed rather than recolored — it's a hypothesis
  // that didn't pan out, not an alarm.
  if (data.confirmationSignal === "refuted") {
    return { stroke: "var(--text-muted)", strokeWidth: 1.5, strokeDasharray: "4 4", opacity: 0.35 };
  }

  const isDashed = DASHED_EDGE_KINDS.has(data.edgeKind);

  // Prediction edges are recolored by the agent that made the prediction —
  // with several agents forecasting concurrently, "who claimed this" is the
  // information a viewer actually needs from the line.
  const stroke = data.edgeKind === "prediction" ? agentColorVar(data.sourceAgent) : EDGE_KIND_COLOR[data.edgeKind];

  const strokeWidth = STRUCTURAL_KINDS.has(data.edgeKind) ? 1 : EMPHASIZED_KINDS.has(data.edgeKind) ? 3 : 1.5;

  return {
    stroke: stroke ?? "var(--text-secondary)",
    strokeWidth,
    ...(isDashed ? { strokeDasharray: "6 4" } : {}),
    ...(STRUCTURAL_KINDS.has(data.edgeKind) ? { opacity: 0.55 } : {}),
  };
}

function CaseEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, markerEnd }: EdgeProps<CaseFlowEdge>) {
  const [edgePath] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const style = edgeStyle(data);
  const confirmDot = data?.confirmationSignal ? CONFIRMATION_STATUS_COLOR[data.confirmationSignal] : undefined;
  const tooltip = data ? `${data.sourceAgent}${data.reason ? `\n${data.reason}` : ""}` : undefined;

  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
      {/* Wider invisible hit-area carrying a native tooltip, drawn on top of
          BaseEdge's own interaction path (BaseEdge always renders its hit
          area last, so an earlier sibling never receives hover) — this is
          what lets the graph itself double as the traceability panel:
          hovering any edge shows the real agent + its actual reasoning
          text, not just a colored line. */}
      {tooltip && (
        <path d={edgePath} fill="none" stroke="transparent" strokeWidth={20}>
          <title>{tooltip}</title>
        </path>
      )}
      {confirmDot && data?.edgeKind === "prediction" && data.confirmationSignal !== "pending" && (
        <circle
          cx={(sourceX + targetX) / 2}
          cy={(sourceY + targetY) / 2}
          r={4}
          fill={confirmDot}
          stroke="var(--surface-1)"
          strokeWidth={1}
        />
      )}
    </>
  );
}

export const edgeTypes = {
  caseEdge: CaseEdge,
};
