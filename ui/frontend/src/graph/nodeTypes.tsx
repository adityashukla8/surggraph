import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { CaseGraphNodeData } from "./types";
import { agentColorVar, NODE_TYPE_ICON, CONFIRMATION_STATUS_COLOR, EVENT_NODE_ACCENT } from "./palette";

export type CaseFlowNode = Node<CaseGraphNodeData, "caseNode">;

// The graph is an at-a-glance map, not a transcript — a full sentence-length
// activity_description (Scene Graph Builder's real output can run to 150+
// chars) blows out the node box and wrecks dagre's layout assumptions
// (layout.ts sizes every box the same). Truncate what's ON the node; the
// full untruncated text is still real and still available via the native
// title tooltip below, never discarded.
const MAX_LABEL_LENGTH = 42;

function truncateLabel(label: string): string {
  return label.length > MAX_LABEL_LENGTH ? `${label.slice(0, MAX_LABEL_LENGTH - 1).trimEnd()}…` : label;
}

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
      title={`${data.label}\n${data.sourceAgent} · ${data.sourceTool} · ${new Date(data.timestamp).toLocaleTimeString()}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: accent }} />
      <div className="case-node__row">
        <span className="case-node__icon" style={{ color: accent }}>
          {NODE_TYPE_ICON[data.entityType] ?? "●"}
        </span>
        <span className="case-node__label">{truncateLabel(data.label)}</span>
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
