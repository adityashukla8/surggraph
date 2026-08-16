import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ReactFlow, Background, Controls, Panel, useReactFlow } from "@xyflow/react";
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
/** Fits the view exactly ONCE, when the first nodes arrive.
 *
 * ReactFlow's `fitView` prop runs on mount, and on mount this graph is still
 * empty — so it fits nothing, and every node that lands afterwards is placed
 * outside the viewport. The trigger node sits at the far left as the layout
 * root, which made it look like it had never been written at all.
 *
 * Deliberately NOT a re-fit on every change: that was removed on purpose
 * because it yanked the viewport while you were inspecting the graph. This
 * fires once and then never again, so panning and zooming stay yours.
 */
function InitialFit({ nodeCount }: { nodeCount: number }) {
  const { fitView } = useReactFlow();
  const done = useRef(false);

  useEffect(() => {
    if (done.current || nodeCount === 0) return;
    done.current = true;
    // A frame later, so the layout pass has positioned the new nodes.
    requestAnimationFrame(() => fitView({ padding: 0.15, duration: 400 }));
  }, [nodeCount, fitView]);

  return null;
}


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
            <InitialFit nodeCount={nodes.length} />
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
