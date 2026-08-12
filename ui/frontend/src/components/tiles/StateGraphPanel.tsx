import { ReactFlow, Background, Controls, MiniMap } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { CaseFlowNode } from "../../graph/nodeTypes";
import { nodeTypes } from "../../graph/nodeTypes";
import type { CaseFlowEdge } from "../../graph/edgeTypes";
import { edgeTypes } from "../../graph/edgeTypes";
import type { ConnectionStatus } from "../../graph/useCaseStateStream";

interface StateGraphPanelProps {
  nodes: CaseFlowNode[];
  edges: CaseFlowEdge[];
  status: ConnectionStatus;
  error: string | null;
}

export function StateGraphPanel({ nodes, edges, status, error }: StateGraphPanelProps) {
  return (
    <div className="tile tile--graph" data-tile="state-graph">
      <div className="tile__header">
        <h3>Living State Graph</h3>
        <span className={`tile__status tile__status--${status}`}>{status}</span>
      </div>
      <div className="tile__body tile__body--graph">
        {status === "disconnected" && nodes.length === 0 ? (
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
            <MiniMap pannable zoomable />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}
