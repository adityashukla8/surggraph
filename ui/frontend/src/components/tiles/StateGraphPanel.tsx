import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ReactFlow, Background, Controls, Panel } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { CaseFlowNode } from "../../graph/nodeTypes";
import { nodeTypes } from "../../graph/nodeTypes";
import type { CaseFlowEdge } from "../../graph/edgeTypes";
import { edgeTypes } from "../../graph/edgeTypes";
import type { ConnectionStatus } from "../../graph/useCaseStateStream";
import { GraphLegend } from "../../graph/Legend";

interface StateGraphPanelProps {
  nodes: CaseFlowNode[];
  edges: CaseFlowEdge[];
  status: ConnectionStatus;
  error: string | null;
}

interface GraphCanvasProps extends StateGraphPanelProps {
  isFullscreen: boolean;
  onToggleFullscreen: () => void;
}

// Shared between the inline tile and the fullscreen portal (below) — same
// nodes/edges/status/error props either way, no duplicate data fetching,
// just a different container. `fitView` (the plain prop, not a recurring
// effect) fits once on mount only — a real bug fix: an earlier version
// re-fit on every node/edge count change, which felt like the graph
// "snapping away" whenever someone was mid-pan/zoom trying to actually
// inspect it.
function GraphCanvas({ nodes, edges, status, error, isFullscreen, onToggleFullscreen }: GraphCanvasProps) {
  return (
    <div className={`tile tile--graph${isFullscreen ? " tile--graph-fullscreen" : ""}`} data-tile="state-graph">
      <div className="tile__header">
        <h3>Autonomous Current + Predicted State Graph</h3>
        <div className="tile__header-actions">
          <span className={`tile__status tile__status--${status}`}>{status}</span>
          <button
            type="button"
            className="tile__expand-button"
            onClick={onToggleFullscreen}
            title={isFullscreen ? "Exit full screen" : "Full screen"}
            aria-label={isFullscreen ? "Exit full screen" : "Full screen"}
          >
            {isFullscreen ? "⤡" : "⤢"}
          </button>
        </div>
      </div>
      <div className="tile__body tile__body--graph">
        {status === "idle" ? (
          <p className="tile__placeholder">No case open yet — press play on the video to start the autonomous workflow.</p>
        ) : status === "disconnected" && nodes.length === 0 ? (
          <div className="tile__error">
            <p>State service unreachable.</p>
            {error && <p className="tile__error-detail">{error}</p>}
          </div>
        ) : (
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <Panel position="bottom-right">
              <GraphLegend />
            </Panel>
          </ReactFlow>
        )}
      </div>
    </div>
  );
}

export function StateGraphPanel(props: StateGraphPanelProps) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    if (!isFullscreen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setIsFullscreen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isFullscreen]);

  if (isFullscreen) {
    // Portal to document.body rather than a plain position:fixed div in
    // place — sidesteps any ancestor's overflow:hidden/transform silently
    // clipping or repositioning the overlay (a real, common CSS gotcha
    // for fixed-position children).
    return createPortal(
      <div className="graph-fullscreen-overlay">
        <GraphCanvas {...props} isFullscreen onToggleFullscreen={() => setIsFullscreen(false)} />
      </div>,
      document.body,
    );
  }

  return <GraphCanvas {...props} isFullscreen={false} onToggleFullscreen={() => setIsFullscreen(true)} />;
}
