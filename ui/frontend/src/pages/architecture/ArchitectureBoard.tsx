import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

// SurgOS architecture, built from real DOM + CSS Grid rather than a hand-laid
// SVG. The reason is not preference: in the SVG version every box position was
// a hardcoded number, so text that grew by one word overlapped its neighbour
// and every label needed a manual corridor. Grid cannot overlap by
// construction, text wraps natively, and the type is real browser type at a
// real size rather than scaled canvas units.
//
// Contents are checked against the live project: every GCP service listed here
// is one that `gcloud services list --enabled` actually reports, and every
// flow step is one the code really performs.

type Kind = "external" | "user" | "compute" | "model" | "data" | "gov" | "ops";

interface Svc {
  name: string;
  role: string;
  logo?: string;
  mono?: string;
  kind: Kind;
  /** Marks the components that never call a model, so the claim is visible. */
  note?: string;
}

function Card({ s, logos, anchor, plain }: { s: Svc; logos: Set<string>; anchor?: string; plain?: boolean }) {
  const has = !!s.logo && logos.has(s.logo);
  return (
    <div className={`ab__card ab__card--${s.kind}`} data-anchor={anchor}>
      <span className={`ab__icon${plain ? " ab__icon--plain" : ""}`} aria-hidden="true">
        {has ? <img src={`/icons/${s.logo}`} alt="" /> : <span className="ab__mono">{s.mono ?? s.name.slice(0, 2)}</span>}
      </span>
      <span className="ab__card-text">
        <b>{s.name}</b>
        <i>{s.role}</i>
        {s.note && <em>{s.note}</em>}
      </span>
    </div>
  );
}

const INGEST: Svc[] = [
  { name: "Cloud Storage", role: "surgical video, annotations, build artifacts", logo: "cloudstorage.png", mono: "GCS", kind: "data" },
  { name: "Cloud Run — frontend", role: "React console + SurgBot panel, SSE stream", logo: "cloudrun.png", kind: "compute", note: "runs as a zero-permission identity" },
];

const SURGGRAPH: Svc[] = [
  { name: "Cloud Run — orchestrator", role: "ADK components", logo: "cloudrun.png", kind: "compute" },
  { name: "Cloud Run — state service", role: "single writer of the Living State Graph", logo: "cloudrun.png", kind: "compute" },
];


const SURGBOT: Svc[] = [
  { name: "Cloud Run — SurgBot relay", role: "WebSocket; screens every turn before a model sees it", logo: "cloudrun.png", kind: "compute" },
  { name: "Agent Runtime (GEAP)", role: "5 separately deployed reasoning engines", logo: "gemin.png", mono: "AR", kind: "compute" },
];


const MODELS: Svc[] = [
  { name: "Vertex AI — Gemini 3.5 Flash", role: "reasoning for all 12 SurgGraph agents and SurgBot", logo: "gemin.png", kind: "model" },
  { name: "Vertex AI — MedGemma 4B", role: "writes the operative record", logo: "medgemma.png", kind: "model" },
  { name: "Vertex AI — MedASR", role: "medical-domain speech recognition", logo: "vertexai.png", kind: "model" },
  { name: "Cloud Text-to-Speech", role: "Chirp 3 HD streaming synthesis", mono: "TTS", kind: "model" },
];

const STATE: Svc[] = [
  { name: "Firestore", role: "Living State Graph — 19 node types, 13 edge kinds", logo: "firestore.png", kind: "data", note: "multi-instance safe, real-time fan-out" },
  { name: "Memory Bank (GEAP)", role: "approved feedback, retrieved by similarity", logo: "gemin.png", mono: "MB", kind: "data" },
];

const GOV: Svc[] = [
  { name: "Model Armor", role: "one template, screens input and output", logo: "modelarmor.png", kind: "gov" },
  { name: "Agent Registry (GEAP)", role: "6 registered services", logo: "gemin.png", mono: "AR", kind: "gov" },
  { name: "Agent Identity (GEAP)", role: "SPIFFE certs on 4 subagents", logo: "gemin.png", mono: "AI", kind: "gov" },
  { name: "IAM", role: "3 least-privilege service accounts", logo: "iam.png", mono: "IAM", kind: "gov" },
];

const OPS: Svc[] = [
  { name: "Cloud Trace", role: "OpenTelemetry GenAI spans", logo: "cloudtrace.png", mono: "CT", kind: "ops" },
  { name: "Cloud Logging", role: "agent and service logs", logo: "cloudlogging.png", mono: "CL", kind: "ops" },
  { name: "Cloud Monitoring", role: "metrics", logo: "cloudmonitoring.png", mono: "CM", kind: "ops" },
  { name: "Cloud Build", role: "10-step pipeline on push to main", logo: "cloudbuild.png", kind: "ops" },
  { name: "Artifact Registry", role: "container images", logo: "artifactregistry.png", mono: "AR", kind: "ops" },
];

const EXT_LEFT: Svc[] = [
  { name: "SAR-RARP50", role: "real robotic prostatectomy video + annotations", logo: "video.png", mono: "▶", kind: "external" },
  { name: "Europe PMC · PubMed · Semantic Scholar", role: "3 live literature APIs, merged by rank fusion", logo: "europepmc.png", mono: "PMC", kind: "external" },
  { name: "HAPI FHIR server", role: "external EHR — DocumentReference + Communication", logo: "fhir.png", mono: "FHIR", kind: "external" },
  { name: "GitHub", role: "push to main triggers the deploy", logo: "github.png", mono: "GH", kind: "external" },
];

const EXT_RIGHT: Svc[] = [
  { name: "Surgeon / QA", role: "reviews the completed case by voice", logo: "surgeon.png", kind: "user" },
];

const LEGEND: { k: Kind | "agent" | "tool" | "det" | "hitl"; label: string }[] = [
  { k: "compute", label: "Compute — Cloud Run / Agent Runtime" },
  { k: "model", label: "Model endpoint — Vertex AI" },
  { k: "data", label: "State & storage" },
  { k: "gov", label: "Governance & security" },
  { k: "ops", label: "Observability & delivery" },
  { k: "external", label: "External — outside Google Cloud" },
  { k: "user", label: "Human in the loop" },
];

/** Flow drawn as a real SVG overlay whose coordinates are MEASURED from the
    rendered DOM, not hardcoded. That is what lets this keep CSS Grid's
    no-overlap layout and still show the request path: move a card, resize the
    window, and every arrow re-routes itself. */
interface Conn {
  n: number;
  from: string;
  to: string;
  /** Short form, shown on hover over the marker. */
  label: string;
  /** Full form, shown in the walkthrough. Both come from here, so the number
      on an arrow and the number in the list can never disagree. */
  desc: string;
  accent?: boolean;
  async?: boolean;
  /** A step where a human decides. */
  hitl?: boolean;
  /** Nudges where the line leaves / enters the card, in px off centre
      (positive = right). Only needed where several edges use the same card
      face and would otherwise be drawn on top of each other. */
  exitDx?: number;
  enterDx?: number;
  /** Route through the gap between the two panels rather than through each
      edge's own midpoint, so several edges leaving the same card share one
      trunk and fork in that gap. Their labels are placed on the forks. */
  trunk?: boolean;
  /** Hand-routed through the layout's own lanes, for the few edges whose
      automatic route would run through a card instead of around it. */
  spec?: (ch: Channels) => RouteSpec;
  /** Anchor the label on this leg of the route (0-based) rather than letting
      it settle on the first clear spot, which on a long route is usually the
      escape from the card — the least informative place for it. */
  labelLeg?: number;
}

const CONNS: Conn[] = [
  { n: 1, from: "dataset", to: "gcs", label: "GCS: Video frames", async: true, desc: "Surgical video lands in Cloud Storage." },
  { n: 2, from: "frontend", to: "orchestrator", label: "POST /cases/open", desc: "The console opens a case; the orchestrator mints a case id and starts both sweeps." },
  { n: 3, from: "orchestrator", to: "gemini", trunk: true, exitDx: -90, enterDx: -120, label: "Gemini 3.5: Vision + Reasoning", desc: "Perception and three Error Detection agents call Gemini 3.5 over 5s windows. Weighted consensus — deterministic, no model — needs 2 of 3 before an error is raised." },
  { n: 4, from: "orchestrator", to: "stateservice", label: "Firestore: Graph persistence", desc: "Every finding is written as a state graph rather than held in memory." },
  { n: 5, from: "stateservice", to: "firestore", labelLeg: 1, spec: (ch) => ({ from: "bottom", to: "top", via: [{ y: ch.gapY - 28 }, { x: ch.surgX }] }), label: "FireStore: transactional write", desc: "A transaction increments a monotonic seq, which drives the live SSE fan-out to the console." },
  { n: 6, from: "orchestrator", to: "litapis", label: "Literature retrieval", desc: "Complication Reasoning formulates its own query; three live APIs are merged by reciprocal rank fusion." },
  { n: 7, from: "orchestrator", to: "medgemma", labelLeg: 1, spec: (ch) => ({ from: "bottom", fromOffset: -90, to: "right", via: [{ y: ch.gapY }, { x: ch.modelsX - 15 }] }), label: "MedGemma: Operative record", desc: "MedGemma drafts the operative record. If it is unavailable Gemini 3.5 drafts it instead, and the node records which model actually wrote it." },
  { n: 8, from: "medgemma", to: "armor", spec: (ch) => ({ from: "bottom", to: "left", via: [{ y: ch.modelStackY }, { x: ch.govX + 15 }] }), label: "ModelArmor: Screen the draft", desc: "Model Armor screens the draft autonomously, before a surgeon is ever offered an Approve button." },
  { n: 9, from: "orchestrator", to: "fhir", label: "FHIR: Verified write + readback", hitl: true, desc: "After the fail-closed Verification Gate and the surgeon's approval, the DocumentReference and any alert are written to a real external EHR and read back to confirm they landed." },
  { n: 10, from: "surgeon", to: "relay", labelLeg: 2, spec: (ch) => ({ from: "right", to: "top", via: [{ x: ch.cloudEdgeX }, { y: ch.ingestGapY }] }), label: "HITL: Voice/text review", desc: "The surgeon opens SurgBot and reviews the completed case by voice or text." },
  { n: 11, from: "relay", to: "medasr", labelLeg: 2, spec: (ch) => ({ from: "left", to: "right", fromOffset: -8, via: [{ x: ch.surgX - 12 }, { y: ch.gapY }, { x: ch.modelsX }] }), label: "MedASR: Transcribe", async: true, desc: "MedASR transcribes the turn using medical-domain speech recognition." },
  { n: 12, from: "relay", to: "armor", labelLeg: 2, spec: (ch) => ({ from: "right", to: "top", via: [{ x: ch.botEdgeX }, { y: ch.gapY }] }), label: "ModelArmor: Screen the turn", desc: "Model Armor template screens the transcript before any model sees it — one policy, both directions." },
  { n: 13, from: "relay", to: "agentruntime", label: "GEAP: async_stream_query", desc: "The root agent dispatches to four specialist subagents on Agent Runtime." },
  { n: 14, from: "agentruntime", to: "firestore", labelLeg: 1, spec: (ch) => ({ from: "bottom", fromOffset: -8, to: "right", via: [{ y: ch.gapY - 20 }, { x: ch.govX }] }), label: "Firestore: Reads completed cases", async: true, desc: "Subagents read the completed case off the same Living State Graph SurgGraph wrote." },
  { n: 15, from: "relay", to: "tts", labelLeg: 2, spec: (ch) => ({ from: "left", to: "right", fromOffset: 8, via: [{ x: ch.surgX + 12 }, { y: ch.gapY + 29 }, { x: ch.modelsX + 15 }] }), label: "Chirp: Speech out", async: true, desc: "Chirp 3 HD streams the reply back to the surgeon." },
  { n: 16, from: "agentruntime", to: "memorybank", labelLeg: 1, spec: (ch) => ({ from: "bottom", fromOffset: 8, to: "right", via: [{ y: ch.gapY + 10 }, { x: ch.govX - 15 }] }), label: "MemoryBank: On approval only", hitl: true, desc: "Only when the surgeon approves the review does captured feedback become durable knowledge." },
  { n: 17, from: "memorybank", to: "gemini", label: "Gemini 3.5: Informs the next case", accent: true, desc: "That feedback is retrieved by similarity and injected as \"advisory contex\" into four SurgGraph agents on the next case — the loop that makes this one system." },
  { n: 18, from: "github", to: "cloudbuild", spec: () => ({ from: "bottom", to: "left", via: [] }), label: "CloudBuild: Push to main", async: true, desc: "A push to main runs the 10-step pipeline and deploys all four Cloud Run services." },
];

interface Pt {
  x: number;
  y: number;
}
interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}
interface Path {
  d: string;
  pts: Pt[];
  lx: number;
  ly: number;
  /** Where the leader line touches the arrow, when the label sits off it. */
  leader?: Pt | null;
  c: Conn;
}

/** Orthogonal route between two measured rectangles, choosing the exit and
    entry sides from their actual relative positions. */
/** The empty lanes the layout leaves between panels. Measured, never pinned,
    so widening a CSS gap moves the lines that thread it. */
interface Channels {
  /** Vertical lane between the SurgGraph and SurgBot panels. */
  surgX: number;
  /** Vertical lane between the Models and State & memory groups. */
  modelsX: number;
  /** Vertical lane between the State & memory and Governance groups. */
  govX: number;
  /** Horizontal band between the panel row and Shared services. */
  gapY: number;
  /** Vertical lane inside the SurgBot panel's own right padding, between its
      cards' right edge and the panel border. */
  botEdgeX: number;
  /** The same lane one level out — the cloud boundary's right padding, clear
      of every card inside it. */
  cloudEdgeX: number;
  /** Horizontal band between the ingest row and the workflow panels. */
  ingestGapY: number;
  /** Horizontal lane across the Models stack, between MedGemma and MedASR.
      It also clears the bottom of the shorter State & memory group, so it
      runs the width of Shared services without meeting a card. */
  modelStackY: number;
}

type Side = "top" | "bottom" | "left" | "right";

/** An explicit orthogonal route. `via` is the list of lanes to thread, in
    order — {x} steps sideways into a vertical lane, {y} drops into a
    horizontal one. Used where the automatic route would cut through a card. */
interface RouteSpec {
  from: Side;
  to: Side;
  /** Along the leaving/entering edge, px from its centre. */
  fromOffset?: number;
  toOffset?: number;
  /** Absolute position on that edge instead — an x for top/bottom, a y for
      left/right. Clamped to the edge, so a lane that drifts past the card
      still leaves from the card. */
  fromAt?: number;
  toAt?: number;
  via: Array<{ x: number } | { y: number }>;
}

function edgePoint(r: { x: number; y: number; w: number; h: number }, side: Side, off = 0): Pt {
  switch (side) {
    case "top": return { x: r.x + r.w / 2 + off, y: r.y };
    case "bottom": return { x: r.x + r.w / 2 + off, y: r.y + r.h };
    case "left": return { x: r.x, y: r.y + r.h / 2 + off };
    case "right": return { x: r.x + r.w, y: r.y + r.h / 2 + off };
  }
}

function routeManual(a: DOMRect, b: DOMRect, o: DOMRect, spec: RouteSpec): { d: string; pts: Pt[]; lx: number; ly: number } {
  const A = { x: a.left - o.left, y: a.top - o.top, w: a.width, h: a.height };
  const B = { x: b.left - o.left, y: b.top - o.top, w: b.width, h: b.height };
  const pin = (p: Pt, r: typeof A, side: Side, at?: number): Pt => {
    if (at === undefined) return p;
    const lo = side === "top" || side === "bottom" ? r.x : r.y;
    const hi = side === "top" || side === "bottom" ? r.x + r.w : r.y + r.h;
    const v = Math.min(Math.max(at, lo + 6), hi - 6);
    return side === "top" || side === "bottom" ? { x: v, y: p.y } : { x: p.x, y: v };
  };
  const start = pin(edgePoint(A, spec.from, spec.fromOffset), A, spec.from, spec.fromAt);
  const end = pin(edgePoint(B, spec.to, spec.toOffset), B, spec.to, spec.toAt);

  const pts: Pt[] = [start];
  let cur = start;
  for (const v of spec.via) {
    cur = "x" in v ? { x: v.x, y: cur.y } : { x: cur.x, y: v.y };
    pts.push(cur);
  }
  // Square up on whichever axis the entry side needs before landing on it.
  const vertical = spec.to === "top" || spec.to === "bottom";
  if (vertical && cur.x !== end.x) pts.push({ x: end.x, y: cur.y });
  if (!vertical && cur.y !== end.y) pts.push({ x: cur.x, y: end.y });
  pts.push(end);

  const d = pts.map((q, i) => `${i === 0 ? "M" : "L"} ${q.x} ${q.y}`).join(" ");
  let best = 1;
  let bestLen = -1;
  for (let i = 1; i < pts.length; i++) {
    const len = Math.abs(pts[i].x - pts[i - 1].x) + Math.abs(pts[i].y - pts[i - 1].y);
    if (len > bestLen) {
      bestLen = len;
      best = i;
    }
  }
  return { d, pts, lx: (pts[best - 1].x + pts[best].x) / 2, ly: (pts[best - 1].y + pts[best].y) / 2 };
}

function route(
  a: DOMRect,
  b: DOMRect,
  o: DOMRect,
  nudge: { exitDx?: number; enterDx?: number; corridorY?: number } = {},
): { d: string; pts: Pt[]; lx: number; ly: number } {
  const A = { x: a.left - o.left, y: a.top - o.top, w: a.width, h: a.height };
  const B = { x: b.left - o.left, y: b.top - o.top, w: b.width, h: b.height };
  const ac = { x: A.x + A.w / 2, y: A.y + A.h / 2 };
  const bc = { x: B.x + B.w / 2, y: B.y + B.h / 2 };
  const dx = bc.x - ac.x;
  const dy = bc.y - ac.y;

  let p1: { x: number; y: number };
  let p2: { x: number; y: number };
  let pts: { x: number; y: number }[];

  if (Math.abs(dx) > Math.abs(dy)) {
    // Predominantly horizontal: leave the side facing the target.
    p1 = { x: dx > 0 ? A.x + A.w : A.x, y: ac.y };
    p2 = { x: dx > 0 ? B.x : B.x + B.w, y: bc.y };
    const mx = (p1.x + p2.x) / 2;
    pts = Math.abs(p1.y - p2.y) < 3 ? [p1, p2] : [p1, { x: mx, y: p1.y }, { x: mx, y: p2.y }, p2];
  } else {
    p1 = { x: ac.x + (nudge.exitDx ?? 0), y: dy > 0 ? A.y + A.h : A.y };
    p2 = { x: bc.x + (nudge.enterDx ?? 0), y: dy > 0 ? B.y : B.y + B.h };
    const my = nudge.corridorY ?? (p1.y + p2.y) / 2;
    pts = Math.abs(p1.x - p2.x) < 3 ? [p1, p2] : [p1, { x: p1.x, y: my }, { x: p2.x, y: my }, p2];
  }

  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  let best = 0;
  let bestLen = -1;
  for (let i = 1; i < pts.length; i++) {
    const len = Math.abs(pts[i].x - pts[i - 1].x) + Math.abs(pts[i].y - pts[i - 1].y);
    if (len > bestLen) {
      bestLen = len;
      best = i;
    }
  }
  const a2 = pts[best - 1];
  const b2 = pts[best];
  return { d, pts, lx: (a2.x + b2.x) / 2, ly: (a2.y + b2.y) / 2 };
}


/** Points along a polyline, evenly by arc length. */
function samplePath(pts: Pt[], n: number): Pt[] {
  const segs: { a: Pt; b: Pt; len: number }[] = [];
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const len = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    segs.push({ a: pts[i - 1], b: pts[i], len });
    total += len;
  }
  if (total === 0) return [pts[0]];
  const out: Pt[] = [];
  for (let k = 0; k <= n; k++) {
    let d = (total * k) / n;
    for (const sg of segs) {
      if (d <= sg.len || sg === segs[segs.length - 1]) {
        const t = sg.len === 0 ? 0 : d / sg.len;
        out.push({ x: sg.a.x + (sg.b.x - sg.a.x) * t, y: sg.a.y + (sg.b.y - sg.a.y) * t });
        break;
      }
      d -= sg.len;
    }
  }
  return out;
}

function overlap(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

/** Nudges apart any two routes that would share the same corridor.
    Orthogonal routing naturally puts several paths on the same gutter line,
    where they draw exactly on top of each other and two distinct steps become
    one indistinguishable stroke. This walks the routes in order and shifts a
    corridor by a few pixels at a time until it no longer coincides with one
    already taken, so parallel flows read as parallel. Endpoints are never
    moved — only the middle of the route — so every arrow still meets its
    card. */
function separateCorridors(paths: Path[]) {
  interface Lane {
    axis: "h" | "v";
    at: number;
    from: number;
    to: number;
  }
  const taken: Lane[] = [];
  const clashes = (l: Lane) =>
    taken.some(
      (t) =>
        t.axis === l.axis &&
        Math.abs(t.at - l.at) < 7 &&
        Math.min(t.to, l.to) - Math.max(t.from, l.from) > 12,
    );

  for (const p of paths) {
    if (p.pts.length < 4) continue;
    if (p.c.trunk || p.c.spec) continue; // sharing / hand-picked lanes stay put
    const a = p.pts[1];
    const b = p.pts[2];
    const horizontal = Math.abs(a.y - b.y) < 1;
    const lane = (at: number): Lane =>
      horizontal
        ? { axis: "h", at, from: Math.min(a.x, b.x), to: Math.max(a.x, b.x) }
        : { axis: "v", at, from: Math.min(a.y, b.y), to: Math.max(a.y, b.y) };

    let at = horizontal ? a.y : a.x;
    if (clashes(lane(at))) {
      for (let step = 1; step <= 8; step++) {
        for (const dir of [1, -1]) {
          const cand = at + dir * step * 9;
          if (!clashes(lane(cand))) {
            at = cand;
            step = 99;
            break;
          }
        }
      }
    }
    if (horizontal) {
      a.y = at;
      b.y = at;
    } else {
      a.x = at;
      b.x = at;
    }
    taken.push(lane(at));
    p.d = p.pts.map((q, i) => `${i === 0 ? "M" : "L"} ${q.x} ${q.y}`).join(" ");
  }
}

/** Places every label in genuinely clear space.
    The earlier heuristic only considered points on or near the line, and in a
    dense board most of a path lies over cards — so it kept settling for
    "least bad" instead of "clear". This searches OUTWARD from the path in
    rings until it finds a position that overlaps nothing, which a
    measurement of the real board says exists for ~52% of positions. When the
    label ends up off its line, a leader connects the two so it is never
    ambiguous which arrow it belongs to. */
function placeLabels(paths: Path[], obstacles: Rect[], bounds: Rect, pill: Map<number, { w: number; h: number }>) {
  const placed: Rect[] = [];
  // 6px of air around the pill so neighbours read as separate, not touching.
  const pad = 6;
  // One-slot runs are placed first. Placement is greedy, and an edge between
  // two stacked cards is a stub barely longer than the pill is tall — every
  // anchor on it yields the same box, so it has exactly one home: the gutter.
  // A long path crossing that same gutter has a dozen other spots, so if it
  // claims the gutter first the stub's label gets exiled to somewhere that
  // reads as belonging to a different arrow. Order is otherwise untouched, so
  // no path that already had a good spot is disturbed.
  const len = (pts: Pt[]) => pts.reduce((t, q, i) => (i ? t + Math.hypot(q.x - pts[i - 1].x, q.y - pts[i - 1].y) : 0), 0);
  const oneSlot = (p: Path) => len(p.pts) < 22 * 2;
  for (const p of [...paths.filter(oneSlot), ...paths.filter((q) => !oneSlot(q))]) {
    const m = pill.get(p.c.n);
    const w = (m?.w ?? 34 + p.c.label.length * 6.6) + pad;
    const h = (m?.h ?? 19) + pad;
    // On a forked edge the stem is shared, so a label sitting on it would not
    // say which fork it belongs to. Anchor on the final leg instead — scanning
    // starts at the top of that leg, which is the gap between the panels.
    const legs =
      p.c.labelLeg !== undefined
        ? p.pts.slice(p.c.labelLeg, p.c.labelLeg + 2)
        : p.c.trunk
          ? p.pts.slice(-2)
          : p.pts;
    let anchors = samplePath(legs, 24);
    if (p.c.labelLeg !== undefined) {
      // Nearest the middle of the run first. A label at the end of a run reads
      // as belonging to whatever it turns into next.
      const mid = (anchors.length - 1) / 2;
      anchors = anchors
        .map((a, i) => ({ a, d: Math.abs(i - mid) }))
        .sort((x, y) => x.d - y.d)
        .map((o) => o.a);
    }
    const clashes = (r: Rect) => {
      if (r.x < bounds.x || r.y < bounds.y || r.x + r.w > bounds.w || r.y + r.h > bounds.h) return true;
      for (const o of obstacles) if (overlap(r, o) > 0) return true;
      for (const o of placed) if (overlap(r, o) > 0) return true;
      return false;
    };

    let best: Pt | null = null;
    let bestCost = Infinity;
    // Rings outward from each anchor on the line; nearest clear wins.
    for (let ring = 0; ring <= 9 && !best; ring++) {
      const dist = ring * 15;
      for (const a of anchors) {
        const tries: Pt[] =
          ring === 0
            ? [a]
            : [
                { x: a.x, y: a.y - dist },
                { x: a.x, y: a.y + dist },
                { x: a.x - dist, y: a.y },
                { x: a.x + dist, y: a.y },
                { x: a.x - dist * 0.7, y: a.y - dist * 0.7 },
                { x: a.x + dist * 0.7, y: a.y - dist * 0.7 },
                { x: a.x - dist * 0.7, y: a.y + dist * 0.7 },
                { x: a.x + dist * 0.7, y: a.y + dist * 0.7 },
              ];
        for (const t of tries) {
          const r: Rect = { x: t.x - w / 2, y: t.y - h / 2, w, h };
          if (clashes(r)) continue;
          const cost = dist;
          if (cost < bestCost) {
            bestCost = cost;
            best = t;
          }
        }
        if (best) break;
      }
    }

    const at = best ?? { x: p.lx, y: p.ly };
    p.lx = at.x;
    p.ly = at.y;
    // Nearest point on the line, for the leader.
    let nx = anchors[0];
    let nd = Infinity;
    for (const a of anchors) {
      const d = Math.hypot(a.x - at.x, a.y - at.y);
      if (d < nd) {
        nd = d;
        nx = a;
      }
    }
    p.leader = nd > 20 ? nx : null;
    placed.push({ x: at.x - w / 2, y: at.y - h / 2, w, h });
  }
}

/** The numbered walkthrough, straight off CONNS — so a step in the list and
    the badge on its arrow can never disagree, wherever it is rendered. */
export function ArchitectureWalkthrough() {
  return (
    <div className="ab__flow">
      <h4 className="ab__flow-title">One case, end to end - architecture flow</h4>
      <ol
        className="ab__flow-list"
        style={{ gridTemplateRows: `repeat(${Math.ceil(CONNS.length / 2)}, auto)` }}
      >
        {CONNS.map((c) => (
          <li key={c.n} className={c.hitl ? "ab__flow-hitl" : c.accent ? "ab__flow-accent" : undefined}>
            <span className="ab__flow-num">{c.n}</span>
            <span>
              <b>{c.label}</b>
              {c.desc}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function ArchitectureBoard() {
  const [logos, setLogos] = useState<Set<string>>(new Set());
  useEffect(() => {
    const all = [...INGEST, ...SURGGRAPH, ...SURGBOT, ...MODELS, ...STATE, ...GOV, ...OPS, ...EXT_LEFT, ...EXT_RIGHT];
    const names = [...new Set(all.map((s) => s.logo).filter(Boolean) as string[])];
    let alive = true;
    Promise.all(
      names.map(
        (f) =>
          new Promise<string | null>((res) => {
            const i = new Image();
            i.onload = () => res(f);
            i.onerror = () => res(null);
            i.src = `/icons/${f}`;
          }),
      ),
    ).then((r) => alive && setLogos(new Set(r.filter(Boolean) as string[])));
    return () => {
      alive = false;
    };
  }, []);

  const stageRef = useRef<HTMLDivElement>(null);
  const [paths, setPaths] = useState<Path[]>([]);
  const [size, setSize] = useState({ w: 0, h: 0 });

  const measure = useCallback(() => {
    const root = stageRef.current;
    if (!root) return;
    const o = root.getBoundingClientRect();
    setSize({ w: o.width, h: o.height });
    const findEl = (id: string) => root.querySelector<HTMLElement>(`[data-anchor="${id}"]`);
    const find = (id: string) => findEl(id)?.getBoundingClientRect();
    // Midpoint of the gap between the panel the edge leaves and the one it
    // enters — measured, so it tracks the layout instead of a pinned y.
    const panelGapY = (from: string, to: string) => {
      const a = findEl(from)?.closest(".ab__panel")?.getBoundingClientRect();
      const b = findEl(to)?.closest(".ab__panel")?.getBoundingClientRect();
      if (!a || !b) return undefined;
      return (a.bottom + b.top) / 2 - o.top;
    };
    const box = (sel: string) => root.querySelector(sel)?.getBoundingClientRect();
    const graph = box(".ab__panel--graph");
    const bot = box(".ab__panel--bot");
    const shared = box(".ab__panel--shared");
    const models = box(".ab__sub--models");
    const state = box(".ab__sub--state");
    const ch: Channels | null =
      graph && bot && shared && models && state
        ? {
            surgX: (graph.right + bot.left) / 2 - o.left,
            modelsX: (models.right + state.left) / 2 - o.left,
            govX: (state.right + (box(".ab__sub--gov")?.left ?? state.right)) / 2 - o.left,
            gapY: (graph.bottom + shared.top) / 2 - o.top,
            modelStackY: ((find("medgemma")?.bottom ?? 0) + (find("medasr")?.top ?? 0)) / 2 - o.top,
            botEdgeX:
              bot.right - parseFloat(getComputedStyle(root.querySelector(".ab__panel--bot")!).paddingRight || "14") / 2 - o.left,
            cloudEdgeX:
              (box(".ab__cloud")?.right ?? 0) -
              parseFloat(getComputedStyle(root.querySelector(".ab__cloud")!).paddingRight || "16") / 2 -
              o.left,
            ingestGapY: ((box(".ab__row--even")?.bottom ?? 0) + (box(".ab__row--band")?.top ?? 0)) / 2 - o.top,
          }
        : null;

    const next: Path[] = [];
    for (const c of CONNS) {
      const a = find(c.from);
      const b = find(c.to);
      if (!a || !b) continue;
      if (c.spec && ch) {
        next.push({ ...routeManual(a, b, o, c.spec(ch)), c });
        continue;
      }
      const corridorY = c.trunk ? panelGapY(c.from, c.to) : undefined;
      next.push({ ...route(a, b, o, { exitDx: c.exitDx, enterDx: c.enterDx, corridorY }), c });
    }
    // Every piece of text a label could sit on top of, not just the cards:
    // panel notes and headings are prose too, and a label over them hides
    // both. Missing these was why labels still landed on the summary lines.
    const obstacles: Rect[] = [
      ...root.querySelectorAll<HTMLElement>(
        ".ab__card, .ab__panel-note, .ab__panel h4, .ab__cloud-head, .ab__sub-title, .ab__ext-title",
      ),
    ].map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - o.left, y: r.top - o.top, w: r.width, h: r.height };
    });
    // Real rendered pill sizes, keyed by step number. On the very first pass
    // nothing is on screen yet and placeLabels falls back to an estimate; the
    // ResizeObserver re-runs this once they exist, and the measured widths win
    // from then on.
    const pill = new Map<number, { w: number; h: number }>();
    root.querySelectorAll<HTMLElement>(".ab__wire-label").forEach((el) => {
      const n = Number(el.querySelector("b")?.textContent);
      const r = el.getBoundingClientRect();
      if (n) pill.set(n, { w: r.width, h: r.height });
    });
    separateCorridors(next);
    placeLabels(next, obstacles, { x: 0, y: 0, w: o.width, h: o.height }, pill);
    setPaths(next);
  }, []);

  useLayoutEffect(() => {
    measure();
    const ro = new ResizeObserver(measure);
    if (stageRef.current) ro.observe(stageRef.current);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure, logos]);

  return (
    <div className="ab" id="surgos-architecture-board">
      <div className="ab__stage" ref={stageRef}>
        <svg className="ab__wires" width={size.w} height={size.h} aria-hidden="true">
          <defs>
            <marker id="ab-arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" fill="#94a3b8" />
            </marker>
            <marker id="ab-arw-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" fill="#7c3aed" />
            </marker>
          </defs>
          {paths.map((p, i) => (
            <g key={i}>
              {p.leader && (
                <line
                  x1={p.leader.x}
                  y1={p.leader.y}
                  x2={p.lx}
                  y2={p.ly}
                  stroke={p.c.accent ? "#7c3aed" : p.c.hitl ? "#c98500" : "#cbd5e1"}
                  strokeWidth={1}
                  strokeDasharray="2 3"
                />
              )}
              <path
                d={p.d}
                fill="none"
                stroke={p.c.accent ? "#7c3aed" : p.c.hitl ? "#c98500" : "#94a3b8"}
                strokeWidth={p.c.accent ? 2 : 1.5}
                strokeDasharray={p.c.async ? "6 4" : p.c.accent ? "7 4" : undefined}
                markerEnd={p.c.accent ? "url(#ab-arw-a)" : "url(#ab-arw)"}
              />
            </g>
          ))}
        </svg>
        <aside className="ab__ext">
          <span className="ab__ext-title">External</span>
          {EXT_LEFT.map((s, i) => (
            <Card key={s.name} s={s} logos={logos} anchor={["dataset", "litapis", "fhir", "github"][i]} plain />
          ))}
        </aside>

        <section className="ab__cloud">
          {/* Outside the boundary, but along the top rather than in a column
              of its own — one card did not justify 236px of width. */}
          <div className="ab__ext-top">
            <span className="ab__ext-title">External</span>
            {EXT_RIGHT.map((s) => (
              <Card key={s.name} s={s} logos={logos} anchor="surgeon" plain />
            ))}
          </div>
          <header className="ab__cloud-head">Google Cloud · liveapi-488810 · us-central1</header>

          {/* Equal-height: the frontend card carries an extra line, so left to
              itself this row rendered two cards of visibly different heights. */}
          <div className="ab__row ab__row--2 ab__row--even">
            {INGEST.map((s, i) => (
              <Card key={s.name} s={s} logos={logos} anchor={i === 0 ? "gcs" : "frontend"} />
            ))}
          </div>

          <div className="ab__row ab__row--2 ab__row--band">
            <div className="ab__panel ab__panel--graph">
              <h4>
                SurgGraph <span>Driven by Cloud Run + Vertex AI</span>
              </h4>
              {SURGGRAPH.map((s, i) => (
                <Card key={s.name} s={s} logos={logos} anchor={i === 0 ? "orchestrator" : "stateservice"} />
              ))}
            </div>

            <div className="ab__panel ab__panel--bot">
              <h4>
                SurgBot <span>Driven by Gemini Enterprise Agent Platform</span>
              </h4>
              {SURGBOT.map((s, i) => (
                <Card key={s.name} s={s} logos={logos} anchor={i === 0 ? "relay" : "agentruntime"} />
              ))}
            </div>
          </div>

          <div className="ab__panel ab__panel--shared">
            <h4>
              Shared services <span>both workflows read and write through these</span>
            </h4>
            <div className="ab__row ab__row--3">
              <div className="ab__sub ab__sub--boxed ab__sub--models">
                <span className="ab__sub-title">Models</span>
                {MODELS.map((s, i) => (
                  <Card key={s.name} s={s} logos={logos} anchor={["gemini", "medgemma", "medasr", "tts"][i]} />
                ))}
              </div>
              <div className="ab__sub ab__sub--boxed ab__sub--state">
                <span className="ab__sub-title">State &amp; memory</span>
                {STATE.map((s, i) => (
                  <Card key={s.name} s={s} logos={logos} anchor={i === 0 ? "firestore" : "memorybank"} />
                ))}
              </div>
              <div className="ab__sub ab__sub--boxed ab__sub--gov">
                <span className="ab__sub-title">Governance</span>
                {GOV.map((s, i) => (
                  <Card key={s.name} s={s} logos={logos} anchor={["armor", "registry", "identity", "iam"][i]} />
                ))}
              </div>
            </div>
          </div>

          <div className="ab__panel ab__panel--ops">
            <h4>
              Observability &amp; delivery <span>spans every service above</span>
            </h4>
            <div className="ab__row ab__row--3">
              {OPS.map((s, i) => (
                <Card key={s.name} s={s} logos={logos} anchor={["trace", "logging", "monitoring", "cloudbuild", "artifacts"][i]} />
              ))}
            </div>
          </div>
        </section>


        <div className="ab__wire-labels" aria-hidden="true">
          {paths
                      .map((p, i) => (
              <span
                key={i}
                className={`ab__wire-label${p.c.accent ? " ab__wire-label--accent" : p.c.hitl ? " ab__wire-label--hitl" : ""}`}
                style={{ left: p.lx, top: p.ly }}
              >
                <b>{p.c.n}</b>
                {p.c.label}
              </span>
            ))}
        </div>
      </div>

      <div className="ab__legend">
        {LEGEND.map((l) => (
          <span key={l.k} className="ab__legend-item">
            <i className={`ab__swatch ab__swatch--${l.k}`} />
            {l.label}
          </span>
        ))}
      </div>

      <ArchitectureWalkthrough />
    </div>
  );
}
