import { useState } from "react";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { CaseGraphNodeData } from "./types";
import { acknowledgeProposal } from "../api/hitl";
import {
  agentColorVar,
  nodeKindColorVar,
  nodeKindOutlineStyle,
  NODE_TYPE_ICON,
  NODE_TYPE_ICON_IMAGE,
  NODE_TYPE_LABEL,
  CONFIRMATION_STATUS_COLOR,
} from "./palette";

export type CaseFlowNode = Node<CaseGraphNodeData, "caseNode">;

function CaseNode({ data }: NodeProps<CaseFlowNode>) {
  const [hitlBusy, setHitlBusy] = useState(false);
  const [hitlError, setHitlError] = useState<string | null>(null);
  // Two independent signals on two channels (see palette.ts): the outline says
  // WHAT KIND of thing this is, the icon says WHICH AGENT produced it.
  const kindColor = nodeKindColorVar(data.nodeType, data.raw.attrs);
  const outlineStyle = nodeKindOutlineStyle(data.nodeType);
  const agentColor = agentColorVar(data.sourceAgent);
  const isPredicted = Boolean(data.predicted);
  const statusColor = data.confirmationSignal ? CONFIRMATION_STATUS_COLOR[data.confirmationSignal] : undefined;
  const hasChildren = (data.childCount ?? 0) > 0;

  return (
    <div
      className={`case-node case-node--${data.nodeType}${isPredicted ? " case-node--predicted" : ""}${
        hasChildren ? " case-node--collapsible" : ""
      }${data.collapsed ? " case-node--collapsed" : ""}`}
      style={{ borderColor: kindColor, borderStyle: outlineStyle, width: data.width }}
      title={
        `${data.label}\n${data.sourceAgent} · ${data.sourceTool} · ${new Date(data.timestamp).toLocaleTimeString()}` +
        (hasChildren ? `\n\nClick to ${data.collapsed ? "expand" : "collapse"} this branch` : "")
      }
    >
      <Handle type="target" position={Position.Left} style={{ background: kindColor }} />
      <div className="case-node__row">
        {NODE_TYPE_ICON_IMAGE[data.nodeType] ? (
          // A real uploaded icon, masked (not a plain <img>) so it can still
          // be recolored — real user report: the per-agent tint these
          // started with read too faint/washed out to see clearly. Flat,
          // high-contrast, theme-aware (var(--text-primary): near-black in
          // light mode, white in dark mode, so it never vanishes against a
          // dark background) instead of the agent color the glyph it
          // replaced used to carry.
          <span
            className="case-node__icon case-node__icon-img"
            style={{
              backgroundColor: "var(--text-primary)",
              maskImage: `url(${NODE_TYPE_ICON_IMAGE[data.nodeType]})`,
              WebkitMaskImage: `url(${NODE_TYPE_ICON_IMAGE[data.nodeType]})`,
            }}
          />
        ) : (
          <span className="case-node__icon" style={{ color: agentColor }}>
            {NODE_TYPE_ICON[data.nodeType] ?? "●"}
          </span>
        )}
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
      {/* Always rendered, because every node has a kind. The tag says WHAT this
          node is without a trip to the legend — the legend still exists for the
          edge kinds and the colour encoding, which a per-node tag can't carry.
          Text, not just the outline hue, so the kind survives a greyscale
          screenshot and a colourblind viewer.
          measureLabel.ts::metaRowWidth measures this row; keep the two in sync
          or nodes will render wider or narrower than dagre reserved for them. */}
      <div className="case-node__meta">
        <span className="case-node__tag" style={{ color: kindColor }}>
          {NODE_TYPE_LABEL[data.nodeType] ?? data.nodeType}
        </span>
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
        {/* Collapse affordance, pushed to the right edge like the actions
            panel's chevron. Only on nodes that actually have a subtree to
            hide — a caret on a leaf would promise something a click cannot
            deliver. Collapsed carries the REAL count of what is hidden, so a
            closed branch can never silently swallow part of the case. */}
        {hasChildren && (
          <span className="case-node__collapse">{data.collapsed ? `▸ ${data.hiddenCount ?? 0}` : "▾"}</span>
        )}
      </div>
      {/* HITL #1. Only on a live proposal the surgeon has not yet answered —
          an escalation has no plan to acknowledge, and a resolved one shows
          its outcome instead of offering the choice again. */}
      {data.nodeType === "corrective_trajectory" && !data.raw.attrs.escalated && (
        <div className="case-node__hitl nodrag nopan">
          {data.acknowledgmentOutcome ? (
            <span className={`case-node__hitl-state case-node__hitl-state--${data.acknowledgmentOutcome}`}>
              {data.acknowledgmentOutcome === "acknowledged" ? "✓ acknowledged — alerts silenced" : "✕ dismissed"}
            </span>
          ) : (
            <>
              {(["acknowledged", "dismissed"] as const).map((outcome) => (
                <button
                  key={outcome}
                  className="case-node__hitl-button"
                  disabled={hitlBusy}
                  onClick={async (e) => {
                    e.stopPropagation();
                    setHitlBusy(true);
                    setHitlError(null);
                    try {
                      await acknowledgeProposal(data.raw.node_id, outcome);
                      // No optimistic update: the SSE stream delivers the real
                      // state once the write lands. Showing it as done before
                      // the server agreed would tell the surgeon their action
                      // took effect when it might not have.
                    } catch (err) {
                      setHitlError(err instanceof Error ? err.message : "failed");
                    } finally {
                      setHitlBusy(false);
                    }
                  }}
                >
                  {outcome === "acknowledged" ? "Acknowledge" : "Dismiss"}
                </button>
              ))}
            </>
          )}
          {hitlError && <span className="case-node__hitl-error">{hitlError}</span>}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: kindColor }} />
    </div>
  );
}

export const nodeTypes = {
  caseNode: CaseNode,
};
