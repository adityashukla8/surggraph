import dagre from "@dagrejs/dagre";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";

const NODE_WIDTH = 190;
const NODE_HEIGHT = 56;

// Auto-layout only, deliberately not hand-tuned — good enough for the
// 15-day build budget. Keyed left-to-right (rankdir LR) so the phase
// sequence reads like a timeline, matching how the demo narrates it.
export function layoutGraph(nodes: CaseFlowNode[], edges: CaseFlowEdge[]): CaseFlowNode[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: 32, ranksep: 90 });

  for (const node of nodes) {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  return nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
    };
  });
}
