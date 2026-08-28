import { useMemo } from "react";
import { ReactFlow, Background, type Node, type Edge, Position, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { recapNodes, recapEdges, surgbotNodes, surgbotEdges, LEFT_CENTER, RIGHT_CENTER, SB_FRAME_BOTTOM } from "./SurgBotFlow";

// One diagram, three tiers: the SurgGraph recap and the real SurgBot flow
// (both reused verbatim from SurgBotFlow.tsx — same nodes/edges, not
// redrawn), plus a new third tier below representing the real dual-write
// feedback layer (plan_v2 §16): Firestore is the system of record, Memory
// Bank is the retrieval index SurgGraph's agents consult. This is the one
// thing SurgBotFlow.tsx doesn't already show — SurgBot's output looping
// back into SurgGraph's own agents on a future case.

const SHARED_CENTER = (LEFT_CENTER + RIGHT_CENTER) / 2;

function rowLayout(count: number, width: number, gap: number, center: number) {
  const total = count * width + (count - 1) * gap;
  const startX = center - total / 2;
  return Array.from({ length: count }, (_, i) => startX + i * (width + gap));
}

const NODE_STYLE_BASE: React.CSSProperties = {
  borderRadius: 12,
  padding: "14px 20px",
  fontFamily: "Poppins, sans-serif",
  fontWeight: 700,
  fontSize: 16,
  border: "1.5px solid #e3e8f0",
  background: "#fff",
  color: "#0b1220",
  textAlign: "center",
};

const LABEL_PILL: React.CSSProperties = {
  fontFamily: "Poppins, sans-serif",
  fontWeight: 700,
  fontSize: 14,
  letterSpacing: "0.08em",
  textTransform: "uppercase",
  textAlign: "center",
  border: "none",
  background: "transparent",
  padding: 0,
};

const SHARED_WIDTH = 260;
const SHARED_GAP = 30;
const [firestoreX, memoryX] = rowLayout(2, SHARED_WIDTH, SHARED_GAP, SHARED_CENTER);

const Y_SHARED_LABEL = SB_FRAME_BOTTOM + 60;
const Y_SHARED = SB_FRAME_BOTTOM + 120;

const SHARED_FRAME_PAD = 50;
const SHARED_FRAME_LEFT = firestoreX - SHARED_FRAME_PAD;
const SHARED_FRAME_RIGHT = memoryX + SHARED_WIDTH + SHARED_FRAME_PAD;
const SHARED_FRAME_TOP = Y_SHARED - 40;
const SHARED_FRAME_BOTTOM = Y_SHARED + 110;

const sharedNodes: Node[] = [
  {
    id: "layer-frame",
    position: { x: SHARED_FRAME_LEFT, y: SHARED_FRAME_TOP },
    data: { label: "" },
    style: {
      width: SHARED_FRAME_RIGHT - SHARED_FRAME_LEFT,
      height: SHARED_FRAME_BOTTOM - SHARED_FRAME_TOP,
      border: "1.5px solid var(--home-green)",
      borderRadius: 18,
      background: "rgba(26, 143, 26, 0.05)",
    },
    draggable: false,
    selectable: false,
    zIndex: -1,
  },
  {
    id: "layer-tag",
    position: { x: SHARED_FRAME_LEFT, y: Y_SHARED_LABEL },
    data: { label: "Layer 3 — The Learning Loop (shared, advisory)" },
    style: { ...LABEL_PILL, width: 480, color: "var(--home-green)" },
    draggable: false,
    selectable: false,
  },
  {
    id: "layer-firestore",
    position: { x: firestoreX, y: Y_SHARED },
    data: {
      label: (
        <div>
          <div>Firestore</div>
          <div style={{ fontWeight: 500, fontSize: 12, opacity: 0.85, marginTop: 3 }}>
            surgbot_feedback — audit trail
          </div>
        </div>
      ) as unknown as string,
    },
    style: { ...NODE_STYLE_BASE, width: SHARED_WIDTH, borderColor: "var(--home-green)" },
    targetPosition: Position.Top,
    sourcePosition: Position.Right,
  },
  {
    id: "layer-memory",
    position: { x: memoryX, y: Y_SHARED },
    data: {
      label: (
        <div>
          <div>Memory Bank</div>
          <div style={{ fontWeight: 500, fontSize: 12, opacity: 0.85, marginTop: 3 }}>
            routed, retrievable knowledge
          </div>
        </div>
      ) as unknown as string,
    },
    style: { ...NODE_STYLE_BASE, width: SHARED_WIDTH, background: "linear-gradient(90deg, #1a8f1a, #4caf50)", color: "#fff", border: "1.5px solid transparent" },
    targetPosition: Position.Left,
    sourcePosition: Position.Top,
  },
];

const solidGreen = { stroke: "var(--home-green)", strokeWidth: 2.5 };
const greenArrow = { type: MarkerType.ArrowClosed, color: "var(--home-green)" };

const sharedEdges: Edge[] = [
  {
    id: "e-sbdoc-firestore",
    source: "sb-doc",
    target: "layer-firestore",
    label: "writes on approval",
    labelStyle: { fill: "var(--home-green)", fontWeight: 700, fontSize: 15 },
    labelBgStyle: { fill: "#fff" },
    animated: true,
    style: solidGreen,
    markerEnd: greenArrow,
  },
  {
    id: "e-firestore-memory",
    source: "layer-firestore",
    target: "layer-memory",
    style: { stroke: "var(--home-green)", strokeWidth: 1.5 },
    markerEnd: greenArrow,
  },
  {
    id: "e-memory-agent",
    source: "layer-memory",
    target: "m-agent-4",
    label: "advisory feedback, 4 agents",
    labelStyle: { fill: "var(--home-green)", fontWeight: 700, fontSize: 15 },
    labelBgStyle: { fill: "#fff" },
    animated: true,
    style: solidGreen,
    markerEnd: greenArrow,
  },
];

const nodes: Node[] = [...recapNodes, ...surgbotNodes, ...sharedNodes];
const edges: Edge[] = [...recapEdges, ...surgbotEdges, ...sharedEdges];

export function CompletePictureFlow({ height = 1000 }: { height?: number }) {
  const fitViewOptions = useMemo(() => ({ padding: 0.05 }), []);
  return (
    <div style={{ height, borderRadius: 14, overflow: "hidden", background: "#f8fafc" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={fitViewOptions}
        minZoom={0.1}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        panOnScroll={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        zoomOnDoubleClick={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} color="#dbe4f0" />
      </ReactFlow>
    </div>
  );
}
