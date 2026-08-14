import { useEffect } from "react";
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

// Rendered as a child of <ReactFlow>, which is what gives useReactFlow() a
// valid context here without a separate <ReactFlowProvider> wrapper (the
// documented @xyflow/react pattern — ReactFlow establishes that context for
// its own children). Re-fits the viewport whenever the node/edge COUNT
// changes — a cheap, real proxy for "the graph actually grew," debounced
// past layout.ts's own 500ms re-layout so this fits to the settled
// positions, not the ones about to be replaced.
function AutoFitView({ nodeCount, edgeCount }: { nodeCount: number; edgeCount: number }) {
  const { fitView } = useReactFlow();
  useEffect(() => {
    const handle = setTimeout(() => {
      void fitView({ padding: 0.08, duration: 400 });
    }, 700);
    return () => clearTimeout(handle);
  }, [nodeCount, edgeCount, fitView]);
  return null;
}

export function StateGraphPanel({ nodes, edges, status, error }: StateGraphPanelProps) {
  return (
    <div className="tile tile--graph" data-tile="state-graph">
      <div className="tile__header">
        <h3>Autonomous Current + Predicted State Graph</h3>
        <span className={`tile__status tile__status--${status}`}>{status}</span>
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
            <AutoFitView nodeCount={nodes.length} edgeCount={edges.length} />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}
