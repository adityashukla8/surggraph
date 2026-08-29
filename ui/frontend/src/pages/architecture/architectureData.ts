// SurgOS — system architecture, drawn to Google Cloud reference-architecture
// conventions rather than as a layered stack.
//
// What those conventions actually are (from Google's own Architecture Center
// diagrams — the RAG/Gemini Enterprise and single-agent ADK references):
//   - An outer "Google Cloud" boundary. Users, external datasets and third
//     party systems sit OUTSIDE it; everything managed sits inside.
//   - Numbered step markers on the arrows, so a reader can walk the request
//     path in order instead of guessing.
//   - Functional SUBSYSTEMS as inner containers, not horizontal bands.
//   - Each node labelled with the GCP product AND the role it plays.
//   - Services are the unit of the diagram. What runs inside a service is
//     annotated on it, not exploded into a box per agent — a diagram that
//     draws all 17 agents individually stops being an architecture diagram
//     and becomes an inventory.
//
// Positions are explicit rather than auto-laid-out: placement carries meaning
// here (shared services sit between the subsystems that share them), and an
// auto-layout would destroy that.

export type NodeKind =
  | "external" // outside Google Cloud
  | "user" // a person
  | "compute" // Cloud Run / Agent Runtime
  | "model" // Vertex AI model endpoint
  | "data" // Firestore / GCS / Memory Bank
  | "governance" // Model Armor, IAM, Registry, Identity
  | "ops"; // Trace / Logging / Monitoring

export interface ArchNode {
  id: string;
  /** GCP product, or the external system's name. */
  label: string;
  /** The role it plays here — Google labels both. */
  role?: string;
  /** Extra detail line, e.g. what runs inside a service. */
  detail?: string[];
  logo?: string;
  mono?: string;
  kind: NodeKind;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ArchBox {
  id: string;
  label: string;
  sub?: string;
  x: number;
  y: number;
  w: number;
  h: number;
  tint: string;
  border: string;
  dashed?: boolean;
  /** Boundary boxes get a heavier, more prominent title. */
  boundary?: boolean;
}

export type Side = "t" | "b" | "l" | "r";

export interface ArchEdge {
  from: string;
  fromSide: Side;
  to: string;
  toSide: Side;
  /** Step number in the walkthrough. Omit for supporting relationships. */
  step?: number;
  label?: string;
  /** Dashed = asynchronous / streamed. */
  async?: boolean;
  /** The learning loop — the edge that makes this one system. */
  accent?: boolean;
  /** Manual mid-point for routing around other elements. */
  bend?: { x?: number; y?: number };
}

export const KIND_COLOR: Record<NodeKind, string> = {
  external: "#64748b",
  user: "#c98500",
  compute: "#2a78d6",
  model: "#0b3a63",
  data: "#7c3aed",
  governance: "#d5578a",
  ops: "#1a8f1a",
};

export const KIND_LABEL: Record<NodeKind, string> = {
  user: "User",
  external: "External system — outside Google Cloud",
  compute: "Compute — Cloud Run / Agent Runtime",
  model: "Model endpoint — Vertex AI",
  data: "State & storage",
  governance: "Governance & security",
  ops: "Observability",
};

export const LEGEND_KINDS: NodeKind[] = ["user", "external", "compute", "model", "data", "governance", "ops"];

export const CANVAS = { w: 1920, h: 1210 };

// --- Containers -----------------------------------------------------------

export const BOXES: ArchBox[] = [
  {
    id: "gcp",
    label: "Google Cloud · liveapi-488810 · us-central1",
    x: 312,
    y: 96,
    w: 1310,
    h: 1050,
    tint: "#f7fafd",
    border: "#94a3b8",
    boundary: true,
  },
  {
    id: "surggraph",
    label: "SurgGraph subsystem — autonomous",
    sub: "Runs a case end to end; no human until the record is ready to sign",
    x: 338,
    y: 286,
    w: 606,
    h: 322,
    tint: "#eef4fc",
    border: "#b9d3f0",
  },
  {
    id: "surgbot",
    label: "SurgBot subsystem — conversational review",
    sub: "A surgeon reviews the case; approved feedback becomes knowledge",
    x: 976,
    y: 286,
    w: 620,
    h: 322,
    tint: "#eef7f1",
    border: "#a7d7bd",
  },
  {
    id: "shared",
    label: "Shared services",
    sub: "Both subsystems read and write through the same state, models and guardrails",
    x: 338,
    y: 664,
    w: 1258,
    h: 336,
    tint: "#f8f6fd",
    border: "#d3c2f5",
  },
];

// --- Nodes ----------------------------------------------------------------
// Externals are placed NEXT TO the subsystem they exchange data with:
// SurgGraph's inputs and outputs on the left, the surgeon on the right beside
// SurgBot. That single choice removes every long crossing edge the earlier
// layout needed.

const N = (n: ArchNode) => n;

export const NODES: ArchNode[] = [
  // ---- Outside Google Cloud, left (SurgGraph's world) -----------------
  N({ id: "dataset", label: "SAR-RARP50", role: "real robotic prostatectomy video", mono: "▶", kind: "external", x: 40, y: 150, w: 234, h: 78 }),
  N({ id: "fhir", label: "HAPI FHIR server", role: "external EHR · operative record + alerts", logo: "fhir.png", mono: "FHIR", kind: "external", x: 40, y: 386, w: 234, h: 82 }),
  N({ id: "litapis", label: "Europe PMC · PubMed", role: "3 live literature APIs", logo: "europepmc.png", mono: "PMC", kind: "external", x: 40, y: 520, w: 234, h: 78 }),
  N({ id: "github", label: "GitHub", role: "push to main triggers deploy", logo: "github.png", mono: "GH", kind: "external", x: 40, y: 1030, w: 234, h: 78 }),

  // ---- Outside Google Cloud, right (SurgBot's world) ------------------
  N({ id: "surgeon", label: "Surgeon / QA", role: "reviews the completed case by voice", mono: "☺", kind: "user", x: 1660, y: 386, w: 226, h: 82 }),

  // ---- Ingestion + presentation ---------------------------------------
  N({ id: "gcs", label: "Cloud Storage", role: "video · annotations · build artifacts", logo: "cloudstorage.png", mono: "GCS", kind: "data", x: 338, y: 150, w: 400, h: 82 }),
  N({ id: "frontend", label: "Cloud Run — frontend", role: "React console + SurgBot panel · SSE", logo: "cloudrun.png", kind: "compute", x: 796, y: 150, w: 400, h: 82 }),

  // ---- SurgGraph subsystem --------------------------------------------
  N({
    id: "orchestrator",
    label: "Cloud Run — orchestrator",
    role: "12 in-process ADK components on an event bus",
    detail: ["8 agents · 1 tool · 3 deterministic", "Perception · Error Detection ×3 · Complication", "Corrective · Divergence · Documentation"],
    logo: "cloudrun.png",
    kind: "compute",
    x: 362,
    y: 350,
    w: 558,
    h: 142,
  }),
  N({ id: "stateservice", label: "Cloud Run — state service", role: "single writer of the Living State Graph", logo: "cloudrun.png", kind: "compute", x: 548, y: 522, w: 372, h: 70 }),

  // ---- SurgBot subsystem ----------------------------------------------
  N({ id: "relay", label: "Cloud Run — SurgBot relay", role: "WebSocket · screens every turn before a model sees it", logo: "cloudrun.png", kind: "compute", x: 1000, y: 350, w: 572, h: 80 }),
  N({
    id: "agentruntime",
    label: "Agent Runtime (GEAP)",
    role: "5 separately deployed reasoning engines",
    detail: ["Root (9 tools) · Error Chain · Synthesis", "Pattern Insight · Feedback Router", "4 hold SPIFFE Agent Identity"],
    logo: "agentengine.png",
    mono: "AR",
    kind: "compute",
    x: 1000,
    y: 452,
    w: 572,
    h: 136,
  }),

  // ---- Shared services -------------------------------------------------
  N({ id: "vertex", label: "Vertex AI — Gemini 3.5", role: "reasoning for all 12 SurgGraph agents", logo: "gemin.png", kind: "model", x: 362, y: 722, w: 386, h: 82 }),
  N({ id: "medgemma", label: "Vertex AI — MedGemma 4B", role: "writes the operative record", logo: "medgemma.png", kind: "model", x: 362, y: 820, w: 386, h: 82 }),
  N({ id: "speech", label: "Vertex AI + Cloud TTS", role: "MedASR in · Chirp 3 HD out", logo: "vertexai.png", kind: "model", x: 362, y: 906, w: 386, h: 76 }),

  N({ id: "firestore", label: "Firestore", role: "Living State Graph · 19 node types, 13 edge kinds", logo: "firestore.png", kind: "data", x: 790, y: 722, w: 396, h: 82 }),
  N({ id: "memorybank", label: "Memory Bank (GEAP)", role: "approved feedback, retrieved by similarity", logo: "memorybank.png", mono: "MB", kind: "data", x: 790, y: 820, w: 396, h: 82 }),

  N({ id: "armor", label: "Model Armor", role: "one template, screens input and output", logo: "modelarmor.png", kind: "governance", x: 1228, y: 722, w: 344, h: 82 }),
  N({ id: "iam", label: "IAM", role: "3 least-privilege service accounts", logo: "iam.png", mono: "IAM", kind: "governance", x: 1228, y: 820, w: 344, h: 82 }),
  N({ id: "registry", label: "Agent Registry + Identity", role: "6 registered services · SPIFFE certs", logo: "agentregistry.png", mono: "AR", kind: "governance", x: 1228, y: 906, w: 344, h: 76 }),

  // ---- Ops + CI/CD -----------------------------------------------------
  N({ id: "ops", label: "Cloud Trace · Logging · Monitoring", role: "OpenTelemetry GenAI spans across every service", logo: "cloudtrace.png", mono: "OPS", kind: "ops", x: 338, y: 1030, w: 596, h: 78 }),
  N({ id: "cicd", label: "Cloud Build → Artifact Registry", role: "10-step pipeline · deploys all 4 services", logo: "cloudbuild.png", kind: "ops", x: 976, y: 1030, w: 620, h: 78 }),
];

// --- Edges ----------------------------------------------------------------
// ONLY the numbered walkthrough. Supporting relationships were drawn before
// and made the middle of the diagram unreadable: the research is explicit
// that past three or four crossings a diagram stops communicating. Anything
// not on this path is stated in the node's own text instead.

export const EDGES: ArchEdge[] = [
  { from: "dataset", fromSide: "r", to: "gcs", toSide: "l", step: 1, label: "video + annotations", async: true },
  { from: "frontend", fromSide: "b", to: "orchestrator", toSide: "t", step: 2, label: "POST /cases/open", bend: { y: 262 } },
  { from: "orchestrator", fromSide: "l", to: "vertex", toSide: "t", step: 3, label: "vision + reasoning", bend: { y: 638 } },
  { from: "orchestrator", fromSide: "b", to: "stateservice", toSide: "t", step: 4, label: "graph patches" },
  { from: "stateservice", fromSide: "b", to: "firestore", toSide: "t", step: 5, label: "transactional write", bend: { y: 638 } },
  { from: "orchestrator", fromSide: "l", to: "litapis", toSide: "r", step: 6, label: "literature retrieval", bend: { x: 300 } },
  { from: "orchestrator", fromSide: "l", to: "fhir", toSide: "r", step: 7, label: "verified write + readback", bend: { x: 322 } },
  { from: "surgeon", fromSide: "l", to: "relay", toSide: "r", step: 8, label: "voice review" },
  { from: "relay", fromSide: "b", to: "agentruntime", toSide: "t", step: 9, label: "async_stream_query" },
  { from: "agentruntime", fromSide: "b", to: "memorybank", toSide: "t", step: 10, label: "on approval only", bend: { y: 638 } },
  { from: "memorybank", fromSide: "b", to: "vertex", toSide: "b", step: 11, label: "feedback informs the next case", accent: true, bend: { y: 1008 } },
  { from: "github", fromSide: "r", to: "cicd", toSide: "l", label: "push to main", async: true, bend: { y: 1069 } },
];
