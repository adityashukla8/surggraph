import dagre from "@dagrejs/dagre";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";
import { measureNodeWidth, metaRowWidth, NODE_HEIGHT, NODE_HEIGHT_WITH_CONTROLS } from "./measureLabel";
import { NODE_TYPE_LABEL } from "./palette";

// Auto-layout, left-to-right so the case reads as a timeline — which is also
// what plan_v2 §4.3's "every node ordered by timestamp" wants visually.
//
// Each node is measured individually (measureLabel.ts) rather than assumed to
// be one fixed size, because §4.1 specifies variable-length nodes. Dagre
// reserves space per node from the width we hand it, so per-node measurement
// is what actually delivers "other nodes adjust around it to not overlap."
//
// In LR mode ranksep drives total graph WIDTH and nodesep drives height within
// a rank. Since node widths now vary, ranksep is the gap BETWEEN ranks rather
// than a proxy for node size, so it can stay tight without causing crowding.
const NODESEP = 40;
const RANKSEP = 85;

export function layoutGraph(nodes: CaseFlowNode[], edges: CaseFlowEdge[]): CaseFlowNode[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "LR", nodesep: NODESEP, ranksep: RANKSEP });

  const widths = new Map<string, number>();
  const heights = new Map<string, number>();
  for (const node of nodes) {
    // Every node now carries a kind tag, so the meta row is always present and
    // can be the wider of the two rows on a short-labelled node.
    const width = measureNodeWidth(
      node.data.label,
      metaRowWidth(
        NODE_TYPE_LABEL[node.data.nodeType] ?? node.data.nodeType,
        node.data.severityBand,
        node.data.confidence !== undefined,
        Boolean(node.data.confirmationSignal),
        // Mirrors CaseNode's caret exactly, including the count, so a node that
        // collapses to "> 14" gets the width that string really needs.
        (node.data.childCount ?? 0) > 0
          ? node.data.collapsed
            ? `> ${node.data.hiddenCount ?? 0}`
            : "v"
          : undefined,
      ),
    );
    // A live proposal renders an acknowledge/dismiss row and is taller.
    const height =
      node.data.nodeType === "corrective_trajectory" && !node.data.raw.attrs.escalated
        ? NODE_HEIGHT_WITH_CONTROLS
        : NODE_HEIGHT;
    widths.set(node.id, width);
    heights.set(node.id, height);
    g.setNode(node.id, { width, height });
  }
  for (const edge of edges) {
    g.setEdge(edge.source, edge.target);
  }

  dagre.layout(g);

  return nodes.map((node) => {
    const pos = g.node(node.id);
    const width = widths.get(node.id) ?? 0;
    return {
      ...node,
      // Carried onto the node so CaseNode renders at exactly the width dagre
      // reserved for it. If the two ever disagree the node visually overflows
      // its own slot, so they must come from the same measurement.
      data: { ...node.data, width },
      position: { x: pos.x - width / 2, y: pos.y - NODE_HEIGHT / 2 },
    };
  });
}
