import { type Node, type Edge } from "@xyflow/react";
import { Position } from "@xyflow/react";
import {
  ARROW,
  DASHED,
  GAP,
  GROUP_PAD_BOTTOM,
  GROUP_PAD_TOP,
  GROUP_PAD_X,
  type Kind,
  KIND_COLOR,
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

// SurgBot — the conversational review workflow, drawn on its own canvas so it
// zooms to fit its own content rather than to SurgGraph's wider diagram.
//
// The two workflows are genuinely connected, not merely adjacent, and both
// ends of that connection are drawn here: SurgBot reads completed cases
// straight off the same Living State Graph SurgGraph wrote, and the feedback
// a surgeon approves flows back into SurgGraph's specialist agents as
// advisory context on the next case. Those two nodes are dashed and neutral
// to mark them as the shared boundary rather than parts of SurgBot itself.

const CENTER_X = 300;

// This diagram is a single vertical chain, so height is what binds fitView —
// not width, as it is for SurgGraph. Every row removed and every gap
// tightened here buys zoom, which is the only thing that makes the labels
// legible in a half-width column. SB_STACK is deliberately tighter than the
// shared GAP.stackY for the same reason.
// TYPE SIZE FOR THIS DIAGRAM ONLY. SurgBot sits in the narrower column and
// is a single vertical chain, so its fitView zoom differs from SurgGraph's —
// which means it needs its own scale to render at a matching apparent size.
// Tune these, not flowTheme's FONT, to change SurgBot's text.
const SB_FONT = {
  /** The chain nodes: inputs, MedASR, Model Armor, approval, Memory Bank. */
  main: 16,
  /** Node subtitles and edge labels. */
  sub: 15,
  /** Agents inside the container. */
  component: 16,
};

const SB_STACK = 90;
const IN_W = 268;
/** MedASR carries a subtitle, so it needs more room than the chain nodes. */
const SPEECH_W = 320;
const NODE_W = 180;
const INNER_W = NODE_W * 2 + GAP.componentX;
const GROUP_W = INNER_W + GROUP_PAD_X * 2;
const GROUP_X = CENTER_X - GROUP_W / 2;
const chainX = CENTER_X - IN_W / 2;

const SUBAGENTS: { name: string; kind: Kind }[] = [
  { name: "Error Chain Reviewer", kind: "agent" },
  { name: "Synthesis", kind: "agent" },
  { name: "Pattern Insight", kind: "agent" },
  { name: "Feedback Router", kind: "agent" },
];
const pairXs = rowLayout(2, NODE_W, GAP.componentX, CENTER_X);

// The two entry points sit side by side, exactly as SurgGraph's two inputs
// do. The state graph appears in both diagrams on purpose: it is genuinely
// one graph, so it carries the SAME label in both, and here it is dashed to
// mark it as a reference to the other diagram rather than a second graph.
const [graphInX, surgeonX] = rowLayout(2, IN_W, GAP.inputX, CENTER_X);
const Y_INPUTS = -15;
const Y_SPEECH = SB_STACK;
const Y_ARMOR = SB_STACK * 2;

const GROUP_H = GROUP_PAD_TOP + 2 * GAP.componentY + NODE_H + GROUP_PAD_BOTTOM;
const GROUP_Y = Y_ARMOR + SB_STACK;
const ROW_ROOT = GROUP_PAD_TOP;
const ROW_A = GROUP_PAD_TOP + GAP.componentY;
const ROW_B = GROUP_PAD_TOP + 2 * GAP.componentY;

const Y_HITL = GROUP_Y + GROUP_H + GAP.afterGroup;
const Y_MEMORY = Y_HITL + SB_STACK;
const Y_SHARED_OUT = Y_MEMORY + SB_STACK;

/** The shared boundary with SurgGraph — dashed, so it reads as "the other
    workflow" rather than as a SurgBot component. */
const boundaryStyle: React.CSSProperties = {
  ...NODE_STYLE_BASE,
  width: IN_W,
  borderColor: "#2a78d6",
  borderStyle: "dashed",
  background: "rgba(42, 120, 214, 0.06)",
  fontSize: SB_FONT.component,
};

const nodes: Node[] = [
  {
    // Same wording as SurgGraph's own node, deliberately: there is ONE state
    // graph, and different wording would suggest two.
    id: "sb-graph-in",
    position: { x: graphInX, y: Y_INPUTS },
    data: { label: "Living State Graph (context layer)" },
    style: boundaryStyle,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-surgeon",
    position: { x: surgeonX, y: Y_INPUTS },
    data: { label: "Surgeon / QA — voice or text" },
    style: { ...NODE_STYLE_BASE, width: IN_W, borderColor: NEUTRAL },
    sourcePosition: Position.Bottom,
  },
  {
    // Same dark treatment as SurgGraph's model node: this is a model layer,
    // not a component — a self-deployed medical-domain ASR endpoint in, and
    // Chirp 3 HD streaming synthesis out. Wider than the chain nodes because
    // it carries a subtitle they do not.
    id: "sb-speech",
    position: { x: CENTER_X - SPEECH_W / 2, y: Y_SPEECH },
    data: {
      label: (
        <div>
          <div>MedASR · Chirp 3 HD</div>
          <div style={{ fontWeight: WEIGHT, fontSize: SB_FONT.sub, opacity: 0.85, marginTop: 3 }}>
            Vertex AI · speech in, speech out
          </div>
        </div>
      ) as unknown as string,
    },
    style: {
      ...NODE_STYLE_BASE,
      width: SPEECH_W,
      background: "linear-gradient(90deg, #0b3a63, #2a78d6)",
      color: "#fff",
      borderColor: "transparent",
      fontSize: SB_FONT.main,
    },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    // One policy template, enforced in both directions: every surgeon turn
    // before it reaches a model, every draft before it can be approved.
    id: "sb-armor",
    position: { x: chainX, y: Y_ARMOR },
    data: { label: "Model Armor" },
    style: { ...NODE_STYLE_BASE, width: IN_W, borderColor: KIND_COLOR.tool, fontSize: SB_FONT.main },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-components",
    position: { x: GROUP_X, y: GROUP_Y },
    // Unlabelled — the header above the canvas carries the platform name.
    data: { label: "" },
    style: containerStyle(GROUP_W, GROUP_H),
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-root",
    parentId: "sb-components",
    position: { x: GROUP_PAD_X, y: ROW_ROOT },
    data: { label: "SurgBot Root Agent" },
    style: { ...componentNodeStyle("agent", INNER_W), fontSize: SB_FONT.component },
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  },
  ...SUBAGENTS.map((c, i) => ({
    id: `sb-sub-${i}`,
    parentId: "sb-components",
    position: { x: pairXs[i % 2] - GROUP_X, y: i < 2 ? ROW_A : ROW_B },
    data: { label: c.name },
    style: { ...componentNodeStyle(c.kind, NODE_W), fontSize: SB_FONT.component },
    targetPosition: Position.Top,
    sourcePosition: Position.Top,
  })),
  {
    id: "sb-hitl",
    position: { x: chainX, y: Y_HITL },
    data: { label: "Review Approval" },
    style: { ...NODE_STYLE_BASE, width: IN_W, borderColor: KIND_COLOR.hitl, fontSize: SB_FONT.main },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-memory",
    position: { x: chainX, y: Y_MEMORY },
    data: { label: "Memory Bank" },
    style: { ...NODE_STYLE_BASE, width: IN_W, borderColor: KIND_COLOR.tool, fontSize: SB_FONT.main },
    targetPosition: Position.Top,
    sourcePosition: Position.Bottom,
  },
  {
    id: "sb-graph-out",
    position: { x: chainX, y: Y_SHARED_OUT },
    data: { label: "SurgGraph agents — next case" },
    style: boundaryStyle,
    targetPosition: Position.Top,
  },
];

const edges: Edge[] = [
  {
    // Unlabelled: the source node already names the graph, and a label here
    // landed on top of the MedASR node.
    id: "e-sb-graph-components",
    source: "sb-graph-in",
    target: "sb-components",
    style: { stroke: "#94a3b8", strokeWidth: 1.5 },
    markerEnd: ARROW,
  },
  { id: "e-sb-surgeon-speech", source: "sb-surgeon", target: "sb-speech", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-sb-speech-armor", source: "sb-speech", target: "sb-armor", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-sb-armor-components", source: "sb-armor", target: "sb-components", animated: true, style: DASHED, markerEnd: ARROW },
  { id: "e-sb-components-hitl", source: "sb-components", target: "sb-hitl", animated: true, style: DASHED, markerEnd: ARROW },
  {
    id: "e-sb-hitl-memory",
    source: "sb-hitl",
    target: "sb-memory",
    label: "approval only",
    labelStyle: edgeLabelStyle,
    labelBgStyle: edgeLabelBg,
    animated: true,
    style: DASHED,
    markerEnd: ARROW,
  },
  {
    // The learning loop closing.
    id: "e-sb-memory-graph",
    source: "sb-memory",
    target: "sb-graph-out",
    label: "feedback informs the next case",
    labelStyle: edgeLabelStyle,
    labelBgStyle: edgeLabelBg,
    animated: true,
    style: { stroke: KIND_COLOR.agent, strokeWidth: 1.5, strokeDasharray: "5 4" },
    markerEnd: { ...ARROW, color: KIND_COLOR.agent },
  },
];

export function SurgBotPipelineFlow({ height = 640 }: { height?: number }) {
  return (
    <FlowCanvas
      nodes={nodes}
      edges={edges}
      height={height}
      title="SurgBot: The Feedback Workflow"
      platform="Powered by Gemini Enterprise Agent Platform"
    />
  );
}
