import { ReactFlow, Background, type Node, type Edge, MarkerType } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// Shared vocabulary for the two workflow diagrams (PipelineFlow = SurgGraph,
// SurgBotPipelineFlow = SurgBot). They render as separate canvases so each
// one's fitView zooms to its own content rather than to the wider of the two,
// but they must look like one system — hence one place for type, spacing,
// colour and the node/legend primitives.

// ---------------------------------------------------------------------
// TYPE SIZE — every knob in one place.
//
// These are pre-zoom values. ReactFlow's fitView scales each canvas to fit
// its container, so what you SEE is `fontSize x zoom`, and zoom is driven by
// that diagram's widest row against its container width. Bumping a size here
// without narrowing the layout can be a no-op: the canvas zooms out to
// compensate. Levers on apparent size, in order of effect:
//   1. node widths / row counts — narrower rows => higher zoom
//   2. the `height` prop — taller container => higher zoom
//   3. the FONT sizes below
// ---------------------------------------------------------------------
export const FONT = {
  /** Inputs, the model layer, the state graph, and the output row. */
  main: 16,
  /** Container labels and edge labels. */
  mainSub: 12,
  /** Nodes inside a container — the smallest text. */
  component: 12,
};

/** Node label weight. Nothing in either diagram is bolded, so no node reads
    as more important than another. */
export const WEIGHT = 500;

/** Spacing. Tightening these also raises apparent type size, since a smaller
    diagram lets fitView zoom in further. */
export const GAP = {
  inputX: 32,
  componentX: 12,
  outputX: 24,
  /** Vertical step down a main chain. */
  stackY: 120,
  /** Vertical step between rows inside a container. */
  componentY: 64,
  /** Above a container. */
  beforeGroup: 110,
  /** Below a container. */
  afterGroup: 70,
};

/** What a node actually IS. Colour-coded, and named in the shared legend. */
export type Kind = "agent" | "tool" | "deterministic" | "hitl" | "action";

// Deliberately not a rainbow: one hue per KIND, so two nodes sharing a colour
// genuinely share a nature.
export const KIND_COLOR: Record<Kind, string> = {
  agent: "#2a78d6",
  tool: "#8b93a1",
  deterministic: "#1a8f1a",
  hitl: "#c98500",
  action: "#d5578a",
};

export const KIND_LABEL: Record<Kind, string> = {
  agent: "Agent",
  tool: "Tool",
  deterministic: "Deterministic",
  hitl: "Human in the loop",
  action: "Action · external write",
};

/** For things that are not components at all — inputs, model layers, state,
    and terminal surfaces. */
export const NEUTRAL = "#94a3b8";

export const NODE_STYLE_BASE: React.CSSProperties = {
  borderRadius: 12,
  padding: "14px 20px",
  fontFamily: "Poppins, sans-serif",
  fontWeight: WEIGHT,
  fontSize: FONT.main,
  border: "1.5px solid #e3e8f0",
  background: "#fff",
  color: "#0b1220",
  textAlign: "center",
};

/** Container padding, shared so both diagrams' grey boxes match. */
export const GROUP_PAD_X = 22;
export const GROUP_PAD_TOP = 18;
export const GROUP_PAD_BOTTOM = 20;
/** Approximate rendered node height, used to size containers. */
export const NODE_H = 56;

export const containerStyle = (w: number, h: number): React.CSSProperties => ({
  width: w,
  height: h,
  background: "rgba(255, 255, 255, 0.6)",
  border: "1.5px dashed #cbd5e1",
  borderRadius: 16,
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "center",
  paddingTop: 12,
  fontFamily: "Poppins, sans-serif",
  fontSize: FONT.mainSub,
  fontWeight: WEIGHT,
  color: "#64748b",
});

export const componentNodeStyle = (kind: Kind, width: number): React.CSSProperties => ({
  ...NODE_STYLE_BASE,
  width,
  fontSize: FONT.component,
  fontWeight: WEIGHT,
  lineHeight: 1.3,
  whiteSpace: "normal",
  padding: "12px 10px",
  borderColor: KIND_COLOR[kind],
  color: "#0b1220",
});

export function rowLayout(count: number, width: number, gap: number, centerX: number) {
  const total = count * width + (count - 1) * gap;
  const startX = centerX - total / 2;
  return Array.from({ length: count }, (_, i) => startX + i * (width + gap));
}

export const DASHED = { stroke: "#94a3b8", strokeDasharray: "4 3" };
export const ARROW = { type: MarkerType.ArrowClosed, color: "#94a3b8" };
export const edgeLabelStyle = { fontSize: FONT.mainSub, fontWeight: WEIGHT, fill: "#64748b" };
export const edgeLabelBg = { fill: "#f8fafc" };

const LEGEND_ORDER: Kind[] = ["agent", "tool", "deterministic", "hitl", "action"];

/** One legend for both diagrams — rendered once by the section, outside the
    canvases, so it can never overlap a node or be clipped by canvas overflow. */
export function FlowLegend() {
  return (
    <div className="home__flow-legend">
      {LEGEND_ORDER.map((k) => (
        <span className="home__flow-legend-item" key={k}>
          <i className="home__flow-legend-swatch" style={{ borderColor: KIND_COLOR[k] }} />
          {KIND_LABEL[k]}
        </span>
      ))}
    </div>
  );
}

/** The grey rounded canvas each workflow sits in. Static: nothing here is
    pannable, zoomable, draggable or selectable — it is a diagram, not a tool. */
export function FlowCanvas({
  nodes,
  edges,
  height,
  title,
  platform,
}: {
  nodes: Node[];
  edges: Edge[];
  height: number;
  title: string;
  platform: string;
}) {
  return (
    <figure className="home__flow-panel" style={{ margin: 0 }}>
      {/* Header above the canvas: naming the workflow and its platform here
          means neither has to be squeezed inside the diagram, which frees the
          container boxes of labels entirely. */}
      <figcaption className="home__flow-caption">
        <span className="home__flow-caption-title">{title}</span>
        <span className="home__flow-caption-platform">{platform}</span>
      </figcaption>
      <div
        className="home__flow-canvas"
        style={{ position: "relative", height, borderRadius: 14, overflow: "hidden", background: "#f8fafc" }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          fitViewOptions={{ padding: 0.08 }}
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
    </figure>
  );
}
