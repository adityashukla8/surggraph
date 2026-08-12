import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { CaseGraphNodeData } from "./types";
import { agentColorVar, NODE_TYPE_ICON, CONFIRMATION_STATUS_COLOR, EVENT_NODE_ACCENT } from "./palette";

export type CaseFlowNode = Node<CaseGraphNodeData, "caseNode">;

function CaseNode({ data }: NodeProps<CaseFlowNode>) {
  const accent =
    data.entityType === "agent"
      ? agentColorVar(data.sourceAgent)
      : data.entityType === "event"
        ? EVENT_NODE_ACCENT
        : "var(--baseline)";
  const isPredicted = Boolean(data.predicted);
  const statusColor = data.confirmationSignal ? CONFIRMATION_STATUS_COLOR[data.confirmationSignal] : undefined;

  return (
    <div
      className={`case-node case-node--${data.entityType}${isPredicted ? " case-node--predicted" : ""}`}
      style={{ borderColor: accent }}
      title={`${data.sourceAgent} · ${data.sourceTool} · ${new Date(data.timestamp).toLocaleTimeString()}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: accent }} />
      <div className="case-node__row">
        <span className="case-node__icon" style={{ color: accent }}>
          {NODE_TYPE_ICON[data.entityType] ?? "●"}
        </span>
        <span className="case-node__label">{data.label}</span>
      </div>
      {(data.confidence !== undefined || statusColor) && (
        <div className="case-node__meta">
          {data.confidence !== undefined && (
            <span className="case-node__confidence">{Math.round(data.confidence * 100)}%</span>
          )}
          {statusColor && (
            <span className="case-node__status-dot" style={{ background: statusColor }} title={data.confirmationSignal} />
          )}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: accent }} />
    </div>
  );
}

export const nodeTypes = {
  caseNode: CaseNode,
};
