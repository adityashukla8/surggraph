import dagre from "@dagrejs/dagre";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";

const NODE_WIDTH = 210; // matches .case-node's fixed CSS width — dagre's spacing math is only accurate if this matches the real rendered size
const NODE_HEIGHT = 56;

// Auto-layout only, deliberately not hand-tuned — good enough for the
// 15-day build budget. Keyed left-to-right (rankdir LR) so the phase
// sequence reads like a timeline, matching how the demo narrates it.
// ranksep close to the ORIGINAL value (not the wider one tried briefly
// this session) — in LR mode ranksep is the dominant driver of total
// graph WIDTH, and a real live test found a wider value made the graph
// too wide to fit the tile without heavy panning at 40+ nodes. The
// original crowding problem was actually caused by unbounded label text
// overflowing each node's box (now fixed via truncation in nodeTypes.tsx),
// not by insufficient dagre spacing — so spacing didn't need to grow much
// once that was fixed. nodesep (vertical, within a rank) stays slightly
// above original for breathing room between sibling nodes.
export function layoutGraph(nodes: CaseFlowNode[], edges: CaseFlowEdge[]): CaseFlowNode[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 85 });

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
