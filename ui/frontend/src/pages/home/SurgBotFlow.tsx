import { useMemo } from "react";
import { ReactFlow, Background, type Node, type Edge, Position, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// The left half recreates PipelineFlow.tsx's real 9-agent SurgGraph diagram
// verbatim (same nodes, same shape), just recolored to monochrome — a
// recap, not a new diagram. The right half is SurgBot's own real flow (root
// agent + its 3 deployed subagents + Cloud Speech-to-Text/Text-to-Speech,
// per agents/surgbot/), drawn in color and linked to the recap by one edge
// off the Living State Graph node — SurgBot reads completed cases from the
// same graph every other agent writes to, it just doesn't drive it.

const LEFT_CENTER = 460;
const RIGHT_CENTER = 1320;

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

const MONO_STYLE_BASE: React.CSSProperties = {
  ...NODE_STYLE_BASE,
  border: "1.5px solid #d3d7de",
  background: "#f4f5f7",
  color: "#6b7280",
  fontWeight: 600,
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

// ------------------------------------------------------------- Left: recap

const MAIN_WIDTH = 300;
const [patientX, videoX] = rowLayout(2, MAIN_WIDTH, 40, LEFT_CENTER);
const mainChainX = LEFT_CENTER - MAIN_WIDTH / 2;

const RECAP_AGENTS = [
  "Perception",
  "Error Detection",
  "Complication Reasoning",
  "Literature Retrieval",
  "Corrective Replanning",
  "Divergence Detection",
  "Alert Routing",
  "Documentation",
  "Verification Gate",
];
const RECAP_ROW1 = RECAP_AGENTS.slice(0, 5);
const RECAP_ROW2 = RECAP_AGENTS.slice(5);
const AGENT_WIDTH = 150;
const AGENT_GAP = 16;
const recapRow1Xs = rowLayout(RECAP_ROW1.length, AGENT_WIDTH, AGENT_GAP, LEFT_CENTER);
const recapRow2Xs = rowLayout(RECAP_ROW2.length, AGENT_WIDTH, AGENT_GAP, LEFT_CENTER);

const OUTPUT_WIDTH = 220;
const [dashboardX, hitlX, fhirX] = rowLayout(3, OUTPUT_WIDTH, 30, LEFT_CENTER);

const Y_LABEL = -90;
const Y_INPUT = 0;
const Y_GEMINI = 140;
const Y_GRAPH = 280;
const Y_AGENTS_ROW1 = 430;
const Y_AGENTS_ROW2 = 530;
const Y_OUTPUT = 680;

const monoAgentStyle: React.CSSProperties = {
  ...MONO_STYLE_BASE,
  width: AGENT_WIDTH,
  fontSize: 13.5,
  lineHeight: 1.3,
  whiteSpace: "normal",
  padding: "12px 12px",
};

const recapNodes: Node[] = [
  {
    id: "m-label",
    position: { x: LEFT_CENTER - 260, y: Y_LABEL },
    data: { label: "SurgGraph — the autonomous pipeline (recap)" },
    style: { ...LABEL_PILL, width: 520, color: "#8b93a1" },
    draggable: false,
  },
  {
    id: "m-patient",
    position: { x: patientX, y: Y_INPUT },
    data: { label: "Patient Data" },
    style: { ...MONO_STYLE_BASE, width: MAIN_WIDTH },
    sourcePosition: Position.Bottom,
  },
  {
    id: "m-video",
    position: { x: videoX, y: Y_INPUT },
    data: { label: "Surgical Video" },
    style: { ...MONO_STYLE_BASE, width: MAIN_WIDTH },
    sourcePosition: Position.Bottom,
  },
  {
    id: "m-gemini",
    position: { x: mainChainX, y: Y_GEMINI },
    data: { label: "Gemini 3.5 — 9 specialist agents" },
    style: {
      ...MONO_STYLE_BASE,
      width: MAIN_WIDTH,
      background: "linear-gradient(90deg, #6b7280, #9ca3af)",
      color: "#fff",
      border: "1.5px solid transparent",
    },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    // Not monochrome, unlike the rest of the recap: this is the real,
    // literal point SurgBot links into (see e-sb-graph-root below), so it
    // stays in its normal color to read as the shared, active connection.
    id: "m-graph",
    position: { x: mainChainX, y: Y_GRAPH },
    data: { label: "Living State Graph (context layer)" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: "var(--home-accent)" },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  ...RECAP_ROW1.map((name, i) => ({
    id: `m-agent-${i}`,
    position: { x: recapRow1Xs[i], y: Y_AGENTS_ROW1 },
    data: { label: name },
    style: monoAgentStyle,
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  ...RECAP_ROW2.map((name, i) => ({
    id: `m-agent-${i + RECAP_ROW1.length}`,
    position: { x: recapRow2Xs[i], y: Y_AGENTS_ROW2 },
    data: { label: name },
    style: monoAgentStyle,
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  {
    id: "m-dashboard",
    position: { x: dashboardX, y: Y_OUTPUT },
    data: { label: "Dashboard" },
    style: { ...MONO_STYLE_BASE, width: OUTPUT_WIDTH },
    targetPosition: Position.Top,
  },
  {
    id: "m-hitl",
    position: { x: hitlX, y: Y_OUTPUT },
    data: { label: "Human in the Loop" },
    style: { ...MONO_STYLE_BASE, width: OUTPUT_WIDTH },
    targetPosition: Position.Top,
  },
  {
    id: "m-fhir",
    position: { x: fhirX, y: Y_OUTPUT },
    data: { label: "FHIR Write" },
    style: { ...MONO_STYLE_BASE, width: OUTPUT_WIDTH },
    targetPosition: Position.Top,
  },
];

const monoDashed = { stroke: "#c3c9d3", strokeDasharray: "4 3" };
const monoArrow = { type: MarkerType.ArrowClosed, color: "#c3c9d3" };

const recapEdges: Edge[] = [
  { id: "e-m-patient-gemini", source: "m-patient", target: "m-gemini", style: monoDashed, markerEnd: monoArrow },
  { id: "e-m-video-gemini", source: "m-video", target: "m-gemini", style: monoDashed, markerEnd: monoArrow },
  { id: "e-m-gemini-graph", source: "m-gemini", target: "m-graph", style: monoDashed, markerEnd: monoArrow },
  ...RECAP_AGENTS.map((_, i) => ({
    id: `e-m-graph-agent-${i}`,
    source: "m-graph",
    target: `m-agent-${i}`,
    style: { stroke: "#d3d7de", strokeWidth: 1.3 },
    markerEnd: monoArrow,
    markerStart: monoArrow,
  })),
  { id: "e-m-graph-dashboard", source: "m-graph", target: "m-dashboard", style: monoDashed, markerEnd: monoArrow },
  { id: "e-m-graph-hitl", source: "m-graph", target: "m-hitl", style: monoDashed, markerEnd: monoArrow },
  { id: "e-m-graph-fhir", source: "m-graph", target: "m-fhir", style: monoDashed, markerEnd: monoArrow },
];

// ---------------------------------------------------------- Right: SurgBot

const SB_MAIN_WIDTH = 300;
const sbInputX = RIGHT_CENTER - SB_MAIN_WIDTH / 2;

const SB_SUB_WIDTH = 150;
const SB_SUB_GAP = 16;
const sbSubXs = rowLayout(3, SB_SUB_WIDTH, SB_SUB_GAP, RIGHT_CENTER);

const SB_OUTPUT_WIDTH = 220;
const [sbDocX, sbTtsX] = rowLayout(2, SB_OUTPUT_WIDTH, 30, RIGHT_CENTER);

const sbSubStyle = (color: string): React.CSSProperties => ({
  ...NODE_STYLE_BASE,
  width: SB_SUB_WIDTH,
  fontSize: 13.5,
  lineHeight: 1.3,
  whiteSpace: "normal",
  padding: "12px 12px",
  borderColor: color,
});

// A thin, real border around SurgBot's half of the diagram (a node like any
// other, so it scales/positions with the same zoom transform as everything
// else) — carries the "Powered by Gemini Enterprise Agent Platform" tag in
// its own top-left corner, per the real GEAP-hosted deployment (§14.3).
const SB_FRAME_PAD_X = 60;
const SB_FRAME_LEFT = Math.min(sbInputX, sbSubXs[0], sbDocX) - SB_FRAME_PAD_X;
const SB_FRAME_RIGHT = Math.max(sbInputX + SB_MAIN_WIDTH, sbSubXs[2] + SB_SUB_WIDTH, sbTtsX + SB_OUTPUT_WIDTH) + SB_FRAME_PAD_X;
const SB_FRAME_TOP = Y_INPUT - 55;
const SB_FRAME_BOTTOM = Y_OUTPUT + 70 + 40;

const surgbotNodes: Node[] = [
  {
    id: "sb-frame",
    position: { x: SB_FRAME_LEFT, y: SB_FRAME_TOP },
    data: { label: "" },
    style: {
      width: SB_FRAME_RIGHT - SB_FRAME_LEFT,
      height: SB_FRAME_BOTTOM - SB_FRAME_TOP,
      border: "1.5px solid var(--home-accent)",
      borderRadius: 18,
      background: "rgba(42, 120, 214, 0.04)",
    },
    draggable: false,
    selectable: false,
    zIndex: -1,
  },
  {
    // Same pill styling as PipelineFlow.tsx's .home__flow-tag (see home.css)
    // — a node, not an absolutely-positioned overlay, since the frame's
    // on-screen position moves with fitView's zoom/pan and can't be pinned
    // with a fixed CSS offset the way a single-diagram tag can.
    id: "sb-frame-tag",
    position: { x: SB_FRAME_LEFT, y: SB_FRAME_TOP - 50 },
    data: { label: "Powered by Gemini Enterprise Agent Platform" },
    style: {
      width: 340,
      fontFamily: "Poppins, sans-serif",
      fontSize: 16,
      fontWeight: 700,
      letterSpacing: "0.04em",
      textTransform: "uppercase",
      color: "var(--home-accent-dark)",
      background: "rgba(255, 255, 255, 0.9)",
      border: "1px solid var(--home-border)",
      borderRadius: 999,
      padding: "5px 12px",
      textAlign: "left",
      whiteSpace: "nowrap",
    },
    draggable: false,
    selectable: false,
  },
  {
    id: "sb-surgeon",
    position: { x: sbInputX, y: Y_INPUT },
    data: { label: "Surgeon / QA Team — voice or text" },
    style: { ...NODE_STYLE_BASE, width: SB_MAIN_WIDTH, borderColor: "var(--home-accent)" },
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-stt",
    position: { x: sbInputX, y: Y_GEMINI },
    data: { label: "Speech-to-Text — Chirp 3" },
    style: { ...NODE_STYLE_BASE, width: SB_MAIN_WIDTH, borderColor: "var(--home-aqua)" },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-root",
    position: { x: sbInputX, y: Y_GRAPH },
    data: {
      label: (
        <div>
          <div>SurgBot Orchestrator</div>
          <div style={{ fontWeight: 500, fontSize: 12.5, opacity: 0.9, marginTop: 3 }}>
            Gemini 3.5 · cross-case, read-only
          </div>
        </div>
      ) as unknown as string,
    },
    style: {
      ...NODE_STYLE_BASE,
      width: SB_MAIN_WIDTH,
      background: "linear-gradient(90deg, #0b3a63, #2a78d6)",
      color: "#fff",
      border: "1.5px solid transparent",
    },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-error-chain",
    position: { x: sbSubXs[0], y: Y_AGENTS_ROW1 },
    data: { label: "Error Chain Review Agent" },
    style: sbSubStyle("var(--home-orange)"),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  },
  {
    id: "sb-synthesis",
    position: { x: sbSubXs[1], y: Y_AGENTS_ROW1 },
    data: { label: "Case Review Agent" },
    style: sbSubStyle("var(--home-magenta)"),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  },
  {
    id: "sb-pattern",
    position: { x: sbSubXs[2], y: Y_AGENTS_ROW1 },
    data: { label: "Cross-cases Coaching Agent" },
    style: sbSubStyle("var(--home-yellow)"),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  },
  {
    id: "sb-doc",
    position: { x: sbDocX, y: Y_OUTPUT },
    data: { label: "Review Document (HITL)" },
    style: { ...NODE_STYLE_BASE, width: SB_OUTPUT_WIDTH, borderColor: "var(--home-violet)" },
    targetPosition: Position.Top,
  },
  {
    id: "sb-tts",
    position: { x: sbTtsX, y: Y_OUTPUT },
    data: { label: "Text-to-Speech — Chirp 3 HD" },
    style: { ...NODE_STYLE_BASE, width: SB_OUTPUT_WIDTH, borderColor: "var(--home-aqua)" },
    targetPosition: Position.Top,
  },
];

const dashed = { stroke: "#94a3b8", strokeDasharray: "4 3" };
const arrow = { type: MarkerType.ArrowClosed, color: "#94a3b8" };
const linkStyle = { stroke: "var(--home-accent)", strokeWidth: 2.5 };
const linkArrow = { type: MarkerType.ArrowClosed, color: "#2a78d6" };

const surgbotEdges: Edge[] = [
  { id: "e-sb-surgeon-stt", source: "sb-surgeon", target: "sb-stt", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-sb-stt-root", source: "sb-stt", target: "sb-root", animated: true, style: dashed, markerEnd: arrow },
  {
    id: "e-sb-graph-root",
    source: "m-graph",
    target: "sb-root",
    label: "reads completed cases",
    labelStyle: { fill: "var(--home-accent)", fontWeight: 700, fontSize: 12 },
    labelBgStyle: { fill: "#fff" },
    animated: true,
    style: linkStyle,
    markerEnd: linkArrow,
  },
  {
    id: "e-sb-root-error-chain",
    source: "sb-root",
    target: "sb-error-chain",
    style: { stroke: "#c7ccd6", strokeWidth: 1.3 },
    markerEnd: arrow,
    markerStart: arrow,
  },
  {
    id: "e-sb-root-synthesis",
    source: "sb-root",
    target: "sb-synthesis",
    style: { stroke: "#c7ccd6", strokeWidth: 1.3 },
    markerEnd: arrow,
    markerStart: arrow,
  },
  {
    id: "e-sb-root-pattern",
    source: "sb-root",
    target: "sb-pattern",
    style: { stroke: "#c7ccd6", strokeWidth: 1.3 },
    markerEnd: arrow,
    markerStart: arrow,
  },
  { id: "e-sb-root-doc", source: "sb-root", target: "sb-doc", animated: true, style: dashed, markerEnd: arrow },
  { id: "e-sb-root-tts", source: "sb-root", target: "sb-tts", animated: true, style: dashed, markerEnd: arrow },
];

const nodes: Node[] = [...recapNodes, ...surgbotNodes];
const edges: Edge[] = [...recapEdges, ...surgbotEdges];

export function SurgBotFlow() {
  const fitViewOptions = useMemo(() => ({ padding: 0.06 }), []);
  return (
    <div style={{ height: 760, borderRadius: 14, overflow: "hidden", background: "#f8fafc" }}>
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
