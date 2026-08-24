import { useEffect, useMemo, useRef, useState } from "react";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";
import type { GraphEdgePatch, GraphNodePatch, ReasoningLogEntry, StateDiffEvent, StateSnapshot } from "./types";
import { layoutGraph } from "./layout";
import { applyCollapse, resetCollapse, useCollapsedNodes } from "./useGraphCollapse";

const STATE_SERVICE_URL = import.meta.env.VITE_STATE_SERVICE_URL ?? "http://localhost:8080";
const LAYOUT_DEBOUNCE_MS = 500;
const RECONNECT_DELAY_MS = 5000;
// `log` is one shared FIFO across every agent's reasoning events, but its
// only consumer (EventInputPanel) filters down to error_detection's alone.
// A 200-entry cap silently evicted that whole subset in a real, reproduced
// run: a case's final phase produces a real burst of divergence/complication/
// alert-routing entries (confirmed live — one run went 200 total events by
// mid-case, then steadily lost 120 down to under 40 real error_detection
// entries as later unrelated agents' activity pushed them out), which read
// as "the feed cleared" even though nothing was actually wrong. 2000 is
// cheap (each entry is a few small fields) and comfortably above what a
// full real case run produces.
const LOG_CAP = 2000;

function toFlowNode(patch: GraphNodePatch): CaseFlowNode {
  return {
    id: patch.node_id,
    type: "caseNode",
    position: { x: 0, y: 0 }, // overwritten by layoutGraph
    data: {
      nodeType: patch.node_type,
      label: patch.label,
      sourceAgent: patch.source_agent,
      sourceTool: patch.source_tool,
      timestamp: patch.timestamp,
      predicted: Boolean(patch.attrs?.predicted),
      severityBand: typeof patch.attrs?.severity_band === "string" ? (patch.attrs.severity_band as string) : undefined,
      externalUrl: typeof patch.attrs?.resource_url === "string" ? (patch.attrs.resource_url as string) : undefined,
      acknowledgmentOutcome: patch.attrs?.acknowledgment_outcome as CaseFlowNode["data"]["acknowledgmentOutcome"],
      confidence: typeof patch.attrs?.confidence === "number" ? (patch.attrs.confidence as number) : undefined,
      confirmationSignal: patch.attrs?.confirmation_signal as CaseFlowNode["data"]["confirmationSignal"],
      raw: patch,
    },
  };
}

function toFlowEdge(patch: GraphEdgePatch): CaseFlowEdge {
  return {
    id: patch.edge_id,
    source: patch.source_node_id,
    target: patch.target_node_id,
    type: "caseEdge",
    data: {
      edgeKind: patch.edge_kind,
      sourceAgent: patch.source_agent,
      trajectoryId: patch.trajectory_id ?? undefined,
      confirmationSignal: patch.confirmation_signal ?? undefined,
      reason: patch.reason,
    },
  };
}

export type ConnectionStatus = "idle" | "connecting" | "connected" | "disconnected";

interface CaseStateStreamResult {
  /** What the canvas draws: collapsed branches removed, laid out by dagre. */
  nodes: CaseFlowNode[];
  edges: CaseFlowEdge[];
  /** The COMPLETE graph, unaffected by collapse. The side panels read these:
   *  hiding a branch is a way of looking at the canvas and must never remove a
   *  citation or a vitals reading from a panel that has nothing to do with it. */
  allNodes: GraphNodePatch[];
  allEdges: CaseFlowEdge[];
  log: ReasoningLogEntry[];
  status: ConnectionStatus;
  error: string | null;
}

/** Streams graph state for `caseId` over SSE, applying each StateDiffEvent to
 * a local node/edge map and re-running dagre layout debounced (not per
 * patch) to avoid jitter.
 *
 * Deliberately fails closed: if the state service is unreachable, this
 * returns empty nodes/edges with status "disconnected" and a populated
 * `error` — it never substitutes fabricated placeholder data, since a
 * graph that looks populated but isn't real would misrepresent what the
 * system is actually doing (see the project's fail-closed design principle
 * in initial_11082026.md §9). Callers must render the disconnected/error
 * state explicitly rather than treating empty nodes as "no case yet."
 *
 * `caseId` is nullable: no case has been opened until the user presses
 * play (services/orchestrator_service's POST /cases/open, triggered from
 * App.tsx's video onPlay handler, not page load) — before that, this
 * intentionally makes no network connection at all and reports "idle",
 * distinct from "connecting"/"disconnected" so the UI can render an
 * honest "nothing has started yet" state rather than implying a stalled
 * or failed connection. */
export function useCaseStateStream(caseId: string | null): CaseStateStreamResult {
  const [nodesById, setNodesById] = useState<Map<string, GraphNodePatch>>(new Map());
  const [edgesById, setEdgesById] = useState<Map<string, GraphEdgePatch>>(new Map());
  const [log, setLog] = useState<ReasoningLogEntry[]>([]);
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  // Per-KEY (not global) last-applied seq. Real finding: Firestore's
  // on_snapshot delivers `changes` in whatever order it batches them in —
  // NOT guaranteed to match our own `seq` field's ascending order, and
  // with three agents (Monitor/Scene Graph Builder/Anticipation) writing
  // concurrently, out-of-seq-order delivery within a batch is routine, not
  // exceptional. A global "next event must be exactly lastSeq+1 or full
  // resync" policy (the earlier version of this hook) treated normal
  // reordering as a fatal gap, causing frequent unnecessary resyncs whose
  // overlapping in-flight fetches could themselves race and leave the
  // graph showing a stale snapshot — the real cause of edges intermittently
  // vanishing. Since every patch is a complete, self-contained SET (not a
  // delta), per-key last-write-wins by seq is correct and reordering-safe:
  // an older patch arriving late for a node/edge that's already been
  // updated by a newer one is simply ignored, regardless of arrival order.
  const nodeSeqById = useRef<Map<string, number>>(new Map());
  const edgeSeqById = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    if (caseId === null) {
      setStatus("idle");
      return;
    }

    let cancelled = false;
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function applySnapshot(snapshot: StateSnapshot) {
      setNodesById(new Map(snapshot.nodes.map((n) => [n.node_id, n])));
      setEdgesById(new Map(snapshot.edges.map((e) => [e.edge_id, e])));
      // The snapshot itself doesn't carry a per-item seq, only the case's
      // overall seq as of the fetch — every item in it is at least that
      // fresh, so any later event with a higher seq legitimately supersedes
      // it, and any event at or below it is already reflected here.
      nodeSeqById.current = new Map(snapshot.nodes.map((n) => [n.node_id, snapshot.seq]));
      edgeSeqById.current = new Map(snapshot.edges.map((e) => [e.edge_id, snapshot.seq]));
    }

    function applyDiff(event: StateDiffEvent) {
      if (event.node && (event.op === "add_node" || event.op === "update_node")) {
        const prevSeq = nodeSeqById.current.get(event.node.node_id) ?? -1;
        if (event.seq > prevSeq) {
          nodeSeqById.current.set(event.node.node_id, event.seq);
          setNodesById((prev) => new Map(prev).set(event.node!.node_id, event.node!));
        }
      }
      if (event.edge && (event.op === "add_edge" || event.op === "update_edge")) {
        const prevSeq = edgeSeqById.current.get(event.edge.edge_id) ?? -1;
        if (event.seq > prevSeq) {
          edgeSeqById.current.set(event.edge.edge_id, event.seq);
          setEdgesById((prev) => new Map(prev).set(event.edge!.edge_id, event.edge!));
        }
      }
      if (event.op === "remove_edge" && event.edge) {
        const prevSeq = edgeSeqById.current.get(event.edge.edge_id) ?? -1;
        if (event.seq > prevSeq) {
          edgeSeqById.current.set(event.edge.edge_id, event.seq);
          setEdgesById((prev) => {
            const next = new Map(prev);
            next.delete(event.edge!.edge_id);
            return next;
          });
        }
      }

      setLog((prev) =>
        [
          {
            event_id: event.event_id,
            timestamp: event.timestamp,
            source_agent: event.source_agent,
            source_tool: event.source_tool,
            reason: event.reason,
            nodeType: event.node?.node_type,
            videoTimeS: typeof event.node?.attrs?.video_time_s === "number" ? (event.node.attrs.video_time_s as number) : undefined,
          },
          ...prev,
        ].slice(0, LOG_CAP),
      );
    }

    /** Fetches the snapshot and returns its seq — the exact point the stream
     *  must resume from. Returning it (rather than discarding it) is what
     *  closes the snapshot/stream gap: see the comment on connect(). */
    async function resync(): Promise<number> {
      const resp = await fetch(`${STATE_SERVICE_URL}/state/${caseId}/snapshot`);
      if (!resp.ok) throw new Error(`snapshot fetch failed: ${resp.status}`);
      const snapshot: StateSnapshot = await resp.json();
      applySnapshot(snapshot);
      return snapshot.seq;
    }

    function scheduleRetry() {
      if (cancelled) return;
      retryTimer = setTimeout(connect, RECONNECT_DELAY_MS);
    }

    async function connect() {
      setStatus("connecting");
      try {
        const seq = await resync();
        if (cancelled) return;
        setStatus("connected");
        setError(null);

        // RESUME FROM THE SNAPSHOT'S OWN SEQ, never from "now".
        //
        // Without since_seq the server baselines the stream at whatever the
        // case seq is when the subscription lands, and everything written
        // between this fetch and that moment reaches neither side — verified
        // against the real service, not inferred. The orchestrator writes the
        // whole case skeleton in one batch right after /cases/open returns,
        // which lands squarely in that window, so the trigger node would
        // sometimes never appear and the aggregation node's absence left every
        // error node orphaned (its edges dropped as dangling).
        es = new EventSource(`${STATE_SERVICE_URL}/state/${caseId}/stream?since_seq=${seq}`);
        es.addEventListener("state_diff", (evt) => {
          applyDiff(JSON.parse((evt as MessageEvent).data));
        });
        es.onerror = () => {
          if (cancelled) return;
          setStatus("disconnected");
          setError("Lost connection to state service — retrying...");
          es?.close();
          scheduleRetry();
        };
      } catch (err) {
        if (cancelled) return;
        setStatus("disconnected");
        setError(err instanceof Error ? err.message : "State service unreachable");
        scheduleRetry();
      }
    }

    void connect();
    return () => {
      cancelled = true;
      es?.close();
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [caseId]);

  const [layoutedNodes, setLayoutedNodes] = useState<CaseFlowNode[]>([]);
  // Node and edge patches arrive as separate, independent SSE events — an
  // edge for a brand-new node pair can legitimately land before (or in the
  // same batch as, in either order) one of its endpoint nodes. ReactFlow
  // silently drops an edge whose source/target isn't in the current nodes
  // array (no error, just invisible) — filtering here means a transiently
  // "orphaned" edge just doesn't render for the one tick until its endpoint
  // catches up, instead of feeding dagre a phantom node and confusing the
  // layout in the meantime.
  const edgesArray = useMemo(() => {
    const nodeIds = new Set(nodesById.keys());
    return Array.from(edgesById.values())
      .filter((e) => nodeIds.has(e.source_node_id) && nodeIds.has(e.target_node_id))
      .map(toFlowEdge);
  }, [edgesById, nodesById]);

  // Collapse is a view of ONE graph. Carrying node ids into the next case
  // would hide arbitrary nodes there, so a new case starts fully expanded.
  useEffect(() => {
    resetCollapse();
  }, [caseId]);

  const collapsedIds = useCollapsedNodes();
  const [visibleEdges, setVisibleEdges] = useState<CaseFlowEdge[]>([]);
  const allNodes = useMemo(() => Array.from(nodesById.values()), [nodesById]);

  useEffect(() => {
    const handle = setTimeout(() => {
      const rawNodes = Array.from(nodesById.values()).map(toFlowNode);
      // Filter BEFORE layout: dagre must reserve space for the visible graph
      // only, so a collapsed branch closes the gap it was occupying instead of
      // leaving a hole where it used to be.
      const view = applyCollapse(rawNodes, edgesArray, collapsedIds);
      setLayoutedNodes(layoutGraph(view.nodes, view.edges));
      setVisibleEdges(view.edges);
    }, LAYOUT_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [nodesById, edgesArray, collapsedIds]);

  return {
    // What the canvas draws: visible only, laid out.
    nodes: layoutedNodes,
    edges: visibleEdges,
    // What the side panels read. Deliberately the COMPLETE graph — collapsing a
    // branch is a way of looking at the canvas, and must never take a citation
    // or a vitals reading out of a panel that has nothing to do with it.
    allNodes,
    allEdges: edgesArray,
    log,
    status,
    error,
  };
}
