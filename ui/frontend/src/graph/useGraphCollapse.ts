// Collapse / expand a node's subtree in the Living Graph.
//
// Clicking a node with children hides everything beneath it; clicking again
// brings it back. The hidden nodes are removed from what dagre lays out, not
// merely made invisible — a collapsed branch that still reserved its space
// would defeat the point, which is to get a dense region out of the way.
//
// State lives here rather than in App for the same reason focus does
// (useGraphFocus.ts): lifting it would re-render every sibling panel on a
// click that only concerns the canvas.

import { useSyncExternalStore } from "react";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";

let collapsed: ReadonlySet<string> = new Set<string>();
const listeners = new Set<() => void>();

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Returns the same reference until a real toggle happens, which is what
// useSyncExternalStore needs to avoid re-rendering on every check.
function getSnapshot(): ReadonlySet<string> {
  return collapsed;
}

export function toggleCollapse(nodeId: string): void {
  const next = new Set(collapsed);
  if (!next.delete(nodeId)) next.add(nodeId);
  collapsed = next;
  listeners.forEach((fn) => fn());
}

/** Drops every collapse. Called when the case changes — collapse is a view of
 *  one specific graph, and carrying ids across cases would hide arbitrary
 *  nodes in the next one. */
export function resetCollapse(): void {
  if (collapsed.size === 0) return;
  collapsed = new Set<string>();
  listeners.forEach((fn) => fn());
}

export function useCollapsedNodes(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export interface CollapsedView {
  nodes: CaseFlowNode[];
  edges: CaseFlowEdge[];
}

/** Hides everything reachable ONLY through a collapsed node, and annotates
 *  each surviving node with what it is hiding.
 *
 *  THIS IS A DAG, NOT A TREE — a node can have several parents, so "descendant
 *  of a collapsed node" is not sufficient grounds to hide it. A literature
 *  paper cited by two different complications must stay on screen when only one
 *  of them is collapsed, or collapsing one branch would silently gut another.
 *
 *  So visibility is decided by reachability, twice: once traversing everything,
 *  once stopping at collapsed nodes. A node disappears only if it is in the
 *  first set and not the second — i.e. every route to it ran through something
 *  collapsed. Anything outside both (an orphan, or a node only reachable around
 *  a cycle) is left alone rather than being hidden by a rule it never met.
 */
export function applyCollapse(
  nodes: CaseFlowNode[],
  edges: CaseFlowEdge[],
  collapsedIds: ReadonlySet<string>,
): CollapsedView {
  const childrenOf = new Map<string, string[]>();
  const inDegree = new Map<string, number>();
  for (const node of nodes) inDegree.set(node.id, 0);
  for (const edge of edges) {
    const kids = childrenOf.get(edge.source);
    if (kids) kids.push(edge.target);
    else childrenOf.set(edge.source, [edge.target]);
    inDegree.set(edge.target, (inDegree.get(edge.target) ?? 0) + 1);
  }

  const childCount = (id: string) => new Set(childrenOf.get(id) ?? []).size;

  // Nothing collapsed: annotate and return untouched. Keeps the default path
  // free of any traversal, and guarantees zero behavioural change from this
  // module until someone actually clicks something.
  if (collapsedIds.size === 0) {
    return {
      nodes: nodes.map((n) => ({ ...n, data: { ...n.data, childCount: childCount(n.id), collapsed: false } })),
      edges,
    };
  }

  const roots = nodes.filter((n) => (inDegree.get(n.id) ?? 0) === 0).map((n) => n.id);

  function reachable(stopAtCollapsed: boolean): Set<string> {
    const seen = new Set<string>();
    const queue = [...roots];
    while (queue.length) {
      const id = queue.pop() as string;
      if (seen.has(id)) continue;
      seen.add(id);
      // A collapsed node is itself visible — it is the handle you click to get
      // its subtree back. Only what lies beyond it is cut off.
      if (stopAtCollapsed && collapsedIds.has(id)) continue;
      for (const child of childrenOf.get(id) ?? []) {
        if (!seen.has(child)) queue.push(child);
      }
    }
    return seen;
  }

  const reachAll = reachable(false);
  const reachStopping = reachable(true);
  const hidden = new Set<string>();
  for (const id of reachAll) if (!reachStopping.has(id)) hidden.add(id);

  /** How many nodes this one is hiding right now — the honest number, counted
   *  against the graph as it currently stands rather than as a static subtree
   *  size, so nested collapses are not double-counted. */
  function hiddenBelow(id: string): number {
    const seen = new Set<string>();
    const queue = [...(childrenOf.get(id) ?? [])];
    while (queue.length) {
      const next = queue.pop() as string;
      if (seen.has(next) || !hidden.has(next)) continue;
      seen.add(next);
      for (const child of childrenOf.get(next) ?? []) if (!seen.has(child)) queue.push(child);
    }
    return seen.size;
  }

  const visibleNodes = nodes
    .filter((n) => !hidden.has(n.id))
    .map((n) => ({
      ...n,
      data: {
        ...n.data,
        childCount: childCount(n.id),
        collapsed: collapsedIds.has(n.id),
        hiddenCount: collapsedIds.has(n.id) ? hiddenBelow(n.id) : undefined,
      },
    }));

  return {
    nodes: visibleNodes,
    edges: edges.filter((e) => !hidden.has(e.source) && !hidden.has(e.target)),
  };
}
