import { BaseEdge, getBezierPath, type EdgeProps, type Edge } from "@xyflow/react";
import type { CaseGraphEdgeData } from "./types";
import { agentColorVar, EDGE_KIND_COLOR, CONFIRMATION_STATUS_COLOR } from "./palette";

export type CaseFlowEdge = Edge<CaseGraphEdgeData, "caseEdge">;

function edgeStyle(data: CaseGraphEdgeData | undefined): React.CSSProperties {
  if (!data) return {};

  if (data.edgeKind === "predicted" && data.confirmationSignal === "refuted") {
    return { stroke: "var(--text-muted)", strokeWidth: 1.5, strokeDasharray: "4 4", opacity: 0.35 };
  }

  switch (data.edgeKind) {
    case "predicted":
      return {
        stroke: agentColorVar(data.sourceAgent),
        strokeWidth: 1.5,
        strokeDasharray: "6 4",
      };
    case "action":
      return { stroke: EDGE_KIND_COLOR.action, strokeWidth: 1.5 };
    case "observed":
      return { stroke: EDGE_KIND_COLOR.observed, strokeWidth: 3 };
    case "revised":
      return { stroke: EDGE_KIND_COLOR.revised, strokeWidth: 3 };
    default:
      return { stroke: "var(--text-secondary)", strokeWidth: 1.5 };
  }
}

function CaseEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, markerEnd }: EdgeProps<CaseFlowEdge>) {
  const [edgePath] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  const style = edgeStyle(data);
  const confirmDot = data?.confirmationSignal ? CONFIRMATION_STATUS_COLOR[data.confirmationSignal] : undefined;

  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
      {confirmDot && data?.edgeKind === "predicted" && data.confirmationSignal !== "pending" && (
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
