import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { CaseGraphNodeData } from "./types";
import {
  agentColorVar,
  nodeKindColorVar,
  nodeKindOutlineStyle,
  NODE_TYPE_ICON,
  CONFIRMATION_STATUS_COLOR,
} from "./palette";

export type CaseFlowNode = Node<CaseGraphNodeData, "caseNode">;

function CaseNode({ data }: NodeProps<CaseFlowNode>) {
  // Two independent signals on two channels (see palette.ts): the outline says
  // WHAT KIND of thing this is, the icon says WHICH AGENT produced it.
  const kindColor = nodeKindColorVar(data.nodeType, data.raw.attrs);
  const outlineStyle = nodeKindOutlineStyle(data.nodeType);
  const agentColor = agentColorVar(data.sourceAgent);
  const isPredicted = Boolean(data.predicted);
  const statusColor = data.confirmationSignal ? CONFIRMATION_STATUS_COLOR[data.confirmationSignal] : undefined;

  return (
    <div
      className={`case-node case-node--${data.nodeType}${isPredicted ? " case-node--predicted" : ""}`}
      style={{ borderColor: kindColor, borderStyle: outlineStyle, width: data.width }}
      title={`${data.label}\n${data.sourceAgent} · ${data.sourceTool} · ${new Date(data.timestamp).toLocaleTimeString()}`}
    >
      <Handle type="target" position={Position.Left} style={{ background: kindColor }} />
      <div className="case-node__row">
        <span className="case-node__icon" style={{ color: agentColor }}>
          {NODE_TYPE_ICON[data.nodeType] ?? "●"}
        </span>
        {/* Label is NOT truncated: plan_v2 §4.1 specifies fixed height with
            length growing to fit the text, and neighbors repositioning around
            it. layout.ts measures the real rendered width and feeds it to
            dagre so growth never causes overlap. */}
        <span className="case-node__label">{data.label}</span>
        {/* A node that reached the real world links to it. Clicking through to
            a third-party FHIR server is what makes an external write
            inspectable rather than merely asserted. nodrag/nopan so the click
            is a click and not the start of a canvas drag. */}
        {data.externalUrl && (
          <a
            className="case-node__external nodrag nopan"
            href={data.externalUrl}
            target="_blank"
            rel="noreferrer"
            title={`Open the real record: ${data.externalUrl}`}
            onClick={(e) => e.stopPropagation()}
          >
            ↗
          </a>
        )}
      </div>
      {(data.confidence !== undefined || statusColor || data.severityBand) && (
        <div className="case-node__meta">
          {/* Severity, not confidence, is what decides whether this error gets
              reasoned about — so it has to be on the node. Two errors at 95%
              confidence can behave completely differently. */}
          {data.severityBand && (
            <span className="case-node__severity" style={{ color: kindColor }}>
              {data.severityBand}
            </span>
          )}
          {data.confidence !== undefined && (
            <span className="case-node__confidence">{Math.round(data.confidence * 100)}%</span>
          )}
          {statusColor && (
            <span className="case-node__status-dot" style={{ background: statusColor }} title={data.confirmationSignal} />
          )}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: kindColor }} />
    </div>
  );
}

export const nodeTypes = {
  caseNode: CaseNode,
};
