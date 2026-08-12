import { useEffect, useMemo, useRef, useState } from "react";
import type { CaseFlowNode } from "./nodeTypes";
import type { CaseFlowEdge } from "./edgeTypes";
import type { GraphEdgePatch, GraphNodePatch, ReasoningLogEntry, StateDiffEvent, StateSnapshot } from "./types";
import { layoutGraph } from "./layout";

const STATE_SERVICE_URL = import.meta.env.VITE_STATE_SERVICE_URL ?? "http://localhost:8080";
const LAYOUT_DEBOUNCE_MS = 500;
const RECONNECT_DELAY_MS = 5000;

function toFlowNode(patch: GraphNodePatch): CaseFlowNode {
  return {
    id: patch.node_id,
    type: "caseNode",
    position: { x: 0, y: 0 }, // overwritten by layoutGraph
    data: {
      entityType: patch.node_type,
      label: patch.label,
      sourceAgent: patch.source_agent,
      sourceTool: patch.source_tool,
      timestamp: patch.timestamp,
      predicted: Boolean(patch.attrs?.predicted),
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

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

interface CaseStateStreamResult {
  nodes: CaseFlowNode[];
  edges: CaseFlowEdge[];
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
 * state explicitly rather than treating empty nodes as "no case yet." */
export function useCaseStateStream(caseId: string): CaseStateStreamResult {
  const [nodesById, setNodesById] = useState<Map<string, GraphNodePatch>>(new Map());
  const [edgesById, setEdgesById] = useState<Map<string, GraphEdgePatch>>(new Map());
  const [log, setLog] = useState<ReasoningLogEntry[]>([]);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const lastSeq = useRef<number>(-1);

  useEffect(() => {
    let cancelled = false;
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function applySnapshot(snapshot: StateSnapshot) {
      setNodesById(new Map(snapshot.nodes.map((n) => [n.node_id, n])));
      setEdgesById(new Map(snapshot.edges.map((e) => [e.edge_id, e])));
      lastSeq.current = snapshot.seq;
    }

    function applyDiff(event: StateDiffEvent) {
      if (event.seq <= lastSeq.current && lastSeq.current !== -1) return; // stale/duplicate
      if (lastSeq.current !== -1 && event.seq > lastSeq.current + 1) {
        // gap detected — resync from a fresh snapshot rather than risk a
        // silently incomplete graph
        void resync();
        return;
      }
      lastSeq.current = event.seq;

      if (event.node && (event.op === "add_node" || event.op === "update_node")) {
        setNodesById((prev) => new Map(prev).set(event.node!.node_id, event.node!));
      }
      if (event.edge && (event.op === "add_edge" || event.op === "update_edge")) {
        setEdgesById((prev) => new Map(prev).set(event.edge!.edge_id, event.edge!));
      }
      if (event.op === "remove_edge" && event.edge) {
        setEdgesById((prev) => {
          const next = new Map(prev);
          next.delete(event.edge!.edge_id);
          return next;
        });
      }

      setLog((prev) =>
        [
          { event_id: event.event_id, timestamp: event.timestamp, source_agent: event.source_agent, source_tool: event.source_tool, reason: event.reason },
          ...prev,
        ].slice(0, 200),
      );
    }

    async function resync() {
      const resp = await fetch(`${STATE_SERVICE_URL}/state/${caseId}/snapshot`);
      if (!resp.ok) throw new Error(`snapshot fetch failed: ${resp.status}`);
      applySnapshot(await resp.json());
    }

    function scheduleRetry() {
      if (cancelled) return;
      retryTimer = setTimeout(connect, RECONNECT_DELAY_MS);
    }

    async function connect() {
      setStatus("connecting");
      try {
        await resync();
        if (cancelled) return;
        setStatus("connected");
        setError(null);

        es = new EventSource(`${STATE_SERVICE_URL}/state/${caseId}/stream`);
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
  const edgesArray = useMemo(() => Array.from(edgesById.values()).map(toFlowEdge), [edgesById]);

  useEffect(() => {
    const handle = setTimeout(() => {
      const rawNodes = Array.from(nodesById.values()).map(toFlowNode);
      setLayoutedNodes(layoutGraph(rawNodes, edgesArray));
    }, LAYOUT_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [nodesById, edgesArray]);

  return { nodes: layoutedNodes, edges: edgesArray, log, status, error };
}
