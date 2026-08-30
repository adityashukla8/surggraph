import { type Node, type Edge } from "@xyflow/react";
import { Position } from "@xyflow/react";
import {
  ARROW,
  DASHED,
  FONT,
  GAP,
  GROUP_PAD_BOTTOM,
  GROUP_PAD_TOP,
  GROUP_PAD_X,
  type Kind,
  NEUTRAL,
  NODE_H,
  NODE_STYLE_BASE,
  WEIGHT,
  componentNodeStyle,
  containerStyle,
  edgeLabelBg,
  edgeLabelStyle,
  rowLayout,
  FlowCanvas,
} from "./flowTheme";

// SurgGraph — the autonomous pipeline, as a real ReactFlow instance (the same
// library the live console graph runs on, see ui/frontend/src/graph/), not a
// CSS mockup.
//
// Components are colour-coded by WHAT THEY ACTUALLY ARE. Only 8 of the 11
// reason with an LLM; Literature Retrieval is a real API tool with no model,
// and Alert Routing and the Verification Gate are deterministic by design —
// consensus arithmetic and a fail-closed safety gate are things a model
// should never be doing. Error Detection is drawn as its three real
// sub-agents rather than one box, because each is a separate Gemini call over
// the same frames from a different angle, and no single one can raise an
// error alone.
//
// The 11 sit inside one container node (a real ReactFlow parent, via
// parentId) so the state graph fans into a single box: one arrow in instead
// of eleven crossing the diagram. The 4/4/3 split keeps the widest row
// narrow — that row drives fitView zoom, so it sets how large every label
// renders — and groups them as perceive, then reason, then act.

const CENTER_X = 460;

const MAIN_WIDTH = 300;
const [patientX, videoX] = rowLayout(2, MAIN_WIDTH, GAP.inputX, CENTER_X);
const mainChainX = CENTER_X - MAIN_WIDTH / 2;

const COMPONENTS: { name: string; kind: Kind }[] = [
  // Perceive
  { name: "Perception Agent", kind: "agent" },
  { name: "Temporal Agent", kind: "agent" },
  { name: "Spatial Agent", kind: "agent" },
  { name: "Procedural Agent", kind: "agent" },
  // Reason
  { name: "Complication Reasoning Agent", kind: "agent" },
  { name: "Literature Retrieval", kind: "tool" },
  { name: "Corrective Replanning Agent", kind: "agent" },
  { name: "Divergence Detection Agent", kind: "agent" },
  // Act
  { name: "Alert Routing", kind: "deterministic" },
  { name: "Documentation Agent", kind: "agent" },
  { name: "Verification Gate", kind: "deterministic" },
];

const ROW1 = COMPONENTS.slice(0, 4);
const ROW2 = COMPONENTS.slice(4, 8);
const ROW3 = COMPONENTS.slice(8);
const NODE_W = 158;
const row1Xs = rowLayout(ROW1.length, NODE_W, GAP.componentX, CENTER_X);
const row2Xs = rowLayout(ROW2.length, NODE_W, GAP.componentX, CENTER_X);
const row3Xs = rowLayout(ROW3.length, NODE_W, GAP.componentX, CENTER_X);

const OUTPUT_WIDTH = 200;
const [dashboardX, hitlX, fhirX] = rowLayout(3, OUTPUT_WIDTH, GAP.outputX, CENTER_X);

const Y_INPUT = 0;
const Y_MODELS = GAP.stackY;
const Y_GRAPH = GAP.stackY * 2;

const GROUP_W = 4 * NODE_W + 3 * GAP.componentX + GROUP_PAD_X * 2;
const GROUP_H = GROUP_PAD_TOP + 2 * GAP.componentY + NODE_H + GROUP_PAD_BOTTOM;
const GROUP_X = CENTER_X - GROUP_W / 2;
const GROUP_Y = Y_GRAPH + GAP.beforeGroup;

const Y_ROW1 = GROUP_PAD_TOP;
const Y_ROW2 = GROUP_PAD_TOP + GAP.componentY;
const Y_ROW3 = GROUP_PAD_TOP + 2 * GAP.componentY;
const Y_OUTPUT = GROUP_Y + GROUP_H + GAP.afterGroup;

const rel = (absX: number) => absX - GROUP_X;
const componentNodes = [
  ...ROW1.map((c, i) => ({ c, x: rel(row1Xs[i]), y: Y_ROW1, i })),
  ...ROW2.map((c, i) => ({ c, x: rel(row2Xs[i]), y: Y_ROW2, i: i + ROW1.length })),
  ...ROW3.map((c, i) => ({ c, x: rel(row3Xs[i]), y: Y_ROW3, i: i + ROW1.length + ROW2.length })),
];

const nodes: Node[] = [
  {
    id: "patient",
    position: { x: patientX, y: Y_INPUT },
    data: { label: "Patient Data" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: NEUTRAL },
    sourcePosition: Position.Bottom,
  },
  {
    id: "video",
    position: { x: videoX, y: Y_INPUT },
    data: { label: "Surgical Video" },
    style: { ...NODE_STYLE_BASE, width: MAIN_WIDTH, borderColor: NEUTRAL },
    sourcePosition: Position.Bottom,
  },
  {
    id: "models",
    position: { x: mainChainX, y: Y_MODELS },
    data: {
      label: (
        <div>
          <div>Gemini 3.5</div>
          <div style={{ fontWeight: WEIGHT, fontSize: FONT.mainSub, opacity: 0.85, marginTop: 3 }}>
            Multimodal · Vertex AI · 8 reasoning agents
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
  {
    // A real ReactFlow parent node, not a drawn rectangle: the 11 components
    // declare it via parentId, so they are positioned relative to it.
    id: "components",
    position: { x: GROUP_X, y: GROUP_Y },
    // Unlabelled: the header above the canvas already names the workflow
    // and its platform, so a label here would only repeat it.
    data: { label: "" },
    style: containerStyle(GROUP_W, GROUP_H),
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  ...componentNodes.map(({ c, x, y, i }) => ({
    id: `agent-${i}`,
    parentId: "components",
    position: { x, y },
    data: { label: c.name },
    style: componentNodeStyle(c.kind, NODE_W),
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  {
    id: "dashboard",
    position: { x: dashboardX, y: Y_OUTPUT },
    data: { label: "Dashboard" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: NEUTRAL },
    targetPosition: Position.Top,
  },
  {
    id: "hitl",
    position: { x: hitlX, y: Y_OUTPUT },
    data: { label: "Human in the Loop" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: "#c98500" },
    targetPosition: Position.Top,
  },
  {
    // The one node that leaves the system: a real DocumentReference /
    // Communication written to a live external FHIR server and read back to
    // verify it landed.
    id: "fhir",
    position: { x: fhirX, y: Y_OUTPUT },
    data: { label: "FHIR Write" },
    style: { ...NODE_STYLE_BASE, width: OUTPUT_WIDTH, borderColor: "#d5578a" },
    targetPosition: Position.Top,
  },
];

const edges: Edge[] = [
  { id: "e-patient-models", source: "patient", target: "models", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-video-models", source: "video", target: "models", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-models-graph", source: "models", target: "graph", animated: true, style: DASHED, markerEnd: ARROW },
  // Bidirectional: every component reads its context from the graph and
  // writes its findings back to it.
  {
    id: "e-graph-components",
    source: "graph",
    target: "components",
    style: { stroke: "#94a3b8", strokeWidth: 1.5 },
    markerEnd: ARROW,
    markerStart: ARROW,
  },
  { id: "e-components-dashboard", source: "components", target: "dashboard", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-components-hitl", source: "components", target: "hitl", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-components-fhir", source: "components", target: "fhir", animated: true, style: DASHED, markerEnd: ARROW },
];

// Kept so the shared caption/edge-label styling is exercised from one place.
void edgeLabelStyle;
void edgeLabelBg;

export function PipelineFlow({ height = 640 }: { height?: number }) {
  return (
    <FlowCanvas
      nodes={nodes}
      edges={edges}
      height={height}
      title="SurgGraph: The Autonomous Workflow"
      platform="Powered by Cloud Run + Vertex AI"
    />
  );
}
