import { useMemo } from "react";
import { ReactFlow, Background, type Node, type Edge, Position, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// A real ReactFlow instance (the same library the actual console graph at
// /console runs on — see ui/frontend/src/graph/), not a CSS mockup, laying
// out the 5 real stages data moves through: patient data + surgical video
// feeding Gemini, the shared Living State Graph every one of the 9 real
// specialist agents reads/writes through, and the 3 real downstream
// surfaces that graph drives — the dashboard, the human-in-the-loop gate,
// and the FHIR write.
//
// The 9-agent layer is deliberately split across two rows (5 + 4) rather
// than one long row of 9 — half as many columns means each node (and its
// label) can render roughly twice as large once ReactFlow's fitView
// computes zoom, since zoom is driven by the widest row in the diagram.

const CENTER_X = 460;

function rowLayout(count: number, width: number, gap: number) {
  const total = count * width + (count - 1) * gap;
  const startX = CENTER_X - total / 2;
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

const MAIN_WIDTH = 300;
const [patientX, videoX] = rowLayout(2, MAIN_WIDTH, 40);
const mainChainX = CENTER_X - MAIN_WIDTH / 2;

const AGENTS = [
  { name: "Perception", color: "var(--home-aqua)" },
  { name: "Error Detection", color: "var(--home-orange)" },
  { name: "Complication Reasoning", color: "var(--home-red)" },
  { name: "Literature Retrieval", color: "var(--home-accent)" },
  { name: "Corrective Replanning", color: "var(--home-magenta)" },
  { name: "Divergence Detection", color: "var(--home-violet)" },
  { name: "Alert Routing", color: "var(--home-yellow)" },
  { name: "Documentation", color: "var(--home-aqua)" },
  { name: "Verification Gate", color: "var(--home-green)" },
];
const AGENTS_ROW1 = AGENTS.slice(0, 5);
const AGENTS_ROW2 = AGENTS.slice(5);
const AGENT_WIDTH = 150;
const AGENT_GAP = 16;
const agentRow1Xs = rowLayout(AGENTS_ROW1.length, AGENT_WIDTH, AGENT_GAP);
const agentRow2Xs = rowLayout(AGENTS_ROW2.length, AGENT_WIDTH, AGENT_GAP);

const OUTPUT_WIDTH = 220;
const [dashboardX, hitlX, fhirX] = rowLayout(3, OUTPUT_WIDTH, 30);

const Y_INPUT = 0;
const Y_GEMINI = 140;
const Y_GRAPH = 280;
const Y_AGENTS_ROW1 = 430;
const Y_AGENTS_ROW2 = 530;
const Y_OUTPUT = 680;

const agentNodeStyle = (color: string): React.CSSProperties => ({
  ...NODE_STYLE_BASE,
  width: AGENT_WIDTH,
  fontSize: 13.5,
  fontWeight: 700,
  lineHeight: 1.3,
  whiteSpace: "normal",
  padding: "12px 12px",
  borderColor: color,
  color: "#0b1220",
});

const nodes: Node[] = [
  {
    id: "patient",
    position: { x: patientX, y: Y_INPUT },
    data: { label: "Patient Data" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: "#59c3e6" },
    sourcePosition: Position.Bottom,
  },
  {
    id: "video",
    position: { x: videoX, y: Y_INPUT },
    data: { label: "Surgical Video" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: "#1baf7a" },
    sourcePosition: Position.Bottom,
  },
  {
    id: "gemini",
    position: { x: mainChainX, y: Y_GEMINI },
    data: {
      label: (
        <div>
          <div>Gemini 3.5</div>
          <div style={{ fontWeight: 500, fontSize: 13, opacity: 0.85, marginTop: 3 }}>
            Multimodal · Vertex AI · 9 specialist agents
          </div>
        </div>
      ) as unknown as string,
    },
    style: {
      ...NODE_STYLE_BASE,
      width: MAIN_WIDTH,
      background: "linear-gradient(90deg, #0b3a63, #2a78d6)",
      color: "#fff",
      borderColor: "transparent",
    },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "graph",
    position: { x: mainChainX, y: Y_GRAPH },
    data: { label: "Living State Graph (context layer)" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: "#2a78d6" },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  ...AGENTS_ROW1.map((a, i) => ({
    id: `agent-${i}`,
    position: { x: agentRow1Xs[i], y: Y_AGENTS_ROW1 },
    data: { label: a.name },
    style: agentNodeStyle(a.color),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  ...AGENTS_ROW2.map((a, i) => ({
    id: `agent-${i + AGENTS_ROW1.length}`,
    position: { x: agentRow2Xs[i], y: Y_AGENTS_ROW2 },
    data: { label: a.name },
    style: agentNodeStyle(a.color),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  {
    id: "dashboard",
    position: { x: dashboardX, y: Y_OUTPUT },
    data: { label: "Dashboard" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: "#4a3aa7" },
    targetPosition: Position.Top,
  },
  {
    id: "hitl",
    position: { x: hitlX, y: Y_OUTPUT },
    data: { label: "Human in the Loop" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: "#2a78d6" },
    targetPosition: Position.Top,
  },
  {
    id: "fhir",
    position: { x: fhirX, y: Y_OUTPUT },
    data: { label: "FHIR Write" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: "#1baf7a" },
    targetPosition: Position.Top,
  },
];

const dashed = { stroke: "#94a3b8", strokeDasharray: "4 3" };
const arrow = { type: MarkerType.ArrowClosed, color: "#94a3b8" };

const edges: Edge[] = [
  { id: "e-patient-gemini", source: "patient", target: "gemini", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-video-gemini", source: "video", target: "gemini", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-gemini-graph", source: "gemini", target: "graph", animated: true, style: dashed, markerEnd: arrow },
  ...AGENTS.map((_, i) => ({
    id: `e-graph-agent-${i}`,
    source: "graph",
    target: `agent-${i}`,
    style: { stroke: "#94a3b8", strokeWidth: 1.3 },
    markerEnd: arrow,
    markerStart: arrow,
  })),
  { id: "e-graph-dashboard", source: "graph", target: "dashboard", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-graph-hitl", source: "graph", target: "hitl", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-graph-fhir", source: "graph", target: "fhir", animated: true, style: dashed, markerEnd: arrow },
];

export function PipelineFlow() {
  const fitViewOptions = useMemo(() => ({ padding: 0.08 }), []);
  return (
    <div style={{ height: 700, borderRadius: 14, overflow: "hidden", background: "#f8fafc" }}>
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
