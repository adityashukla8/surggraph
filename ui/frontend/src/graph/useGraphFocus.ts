// Shared "focus this node" channel between the timeline and the graph.
//
// The timeline needs to point at a node in a component it does not own and is
// not a parent of. Lifting the state to App would make every keystroke in a
// sibling panel re-render the graph, so this is a tiny event bus instead:
// subscribers register a callback, the timeline fires a node id, done.

type Listener = (nodeId: string) => void;

const listeners = new Set<Listener>();

/** Ask the graph to pan to a node and highlight it. Safe to call when nothing
 *  is listening — a click on a row whose graph is not mounted is a no-op, not
 *  an error. */
export function focusNode(nodeId: string): void {
  listeners.forEach((fn) => fn(nodeId));
}

export function onFocusNode(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** How long the highlight pulse lasts. Long enough to find the node with your
 *  eye, short enough that a stale highlight never lingers into the next one. */
export const FOCUS_PULSE_MS = 3000;
