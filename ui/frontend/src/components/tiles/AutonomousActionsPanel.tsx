import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphNodePatch } from "../../graph/types";
import { focusNode } from "../../graph/useGraphFocus";
import { acknowledgeProposal, approveDocumentation } from "../../api/hitl";

// Autonomous Actions, Alerts & HITL Approvals — the action surface.
//
// Timeline-first because the panel is narrow: one compact row per item, detail
// on demand rather than always visible. Newest at the top. Resolved items stay
// in place with their outcome shown — the timeline IS the history, so there is
// no separate resolved section and nothing ever disappears.
//
// Everything here is derived from graph nodes delivered by the same SSE stream
// that drives the graph. No polling, and no state of its own beyond which row
// is open and which button is mid-flight.
//
// HITL POSTS GO TO THE ORCHESTRATOR, not to the state service's generic
// manual-event endpoint. That endpoint accepts only free text and would record
// that a human clicked something while doing none of the work: acknowledging
// would not silence the alert path, and approving would not run the
// verification gate or write to FHIR. A control that reports success while
// changing nothing is the worst outcome available here.

/** The evidence relationship lives in EDGES, not on the literature node — a
 *  paper knows nothing about what cites it. Passing only nodes made every
 *  citation list render empty, which read as "not literature-grounded" for
 *  complications that were in fact grounded. */
export interface TimelineEdge {
  source: string;
  target: string;
  edgeKind: string;
}

interface Props {
  caseId: string | null;
  nodes: GraphNodePatch[];
  edges: TimelineEdge[];
}

type Severity = "low" | "medium" | "high";
type ItemKind = "proposal" | "alert" | "documentation" | "benchmark";

/** The category label shown as a pastel tag. Colours follow the graph's own
 *  node-kind palette so an item here and its node there read as the same
 *  thing — yellow for corrective proposals, red for divergence, blue for the
 *  record, brown for the post-case scorecard. */
const KIND_LABEL: Record<ItemKind, string> = {
  proposal: "Corrective Proposal",
  alert: "Divergence Alert",
  documentation: "Operative Note",
  benchmark: "Self-Benchmark",
};

interface TimelineItem {
  id: string;
  kind: ItemKind;
  at: string;
  severity: Severity;
  summary: string;
  outcome: string | null; // set once resolved; shown in the collapsed row
  node: GraphNodePatch;
}

const KIND_ICON: Record<ItemKind, string> = { proposal: "⇢", alert: "⚠", documentation: "▤", benchmark: "▦" };

function timeOf(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** A single short chime, synthesised rather than loaded.
 *
 * No audio asset to ship or source, and nothing to fail to load. Only ever
 * fires for divergence alerts — a sound for anything more common would train
 * the room to ignore it. */
function chime() {
  try {
    const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.06, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.35);
  } catch {
    // Audio is a nicety. A blocked AudioContext must never break the panel.
  }
}

function buildTimeline(nodes: GraphNodePatch[]): TimelineItem[] {
  const items: TimelineItem[] = [];

  for (const n of nodes) {
    const a = n.attrs ?? {};

    if (n.node_type === "corrective_trajectory") {
      // An escalation is a decision not to propose, so there is nothing for a
      // surgeon to acknowledge — it belongs on the graph, not in this queue.
      if (a.escalated) continue;
      const ack = a.acknowledgment_outcome as string | undefined;
      items.push({
        id: n.node_id,
        kind: "proposal",
        at: n.timestamp,
        severity: a.urgency === "immediate" ? "high" : "medium",
        summary: `Corrective proposal · ${n.label}`,
        outcome: ack ? `${ack === "acknowledged" ? "Acknowledged" : "Dismissed"} ${a.acknowledged_at ? timeOf(a.acknowledged_at as string) : ""}`.trim() : null,
        node: n,
      });
    }

    if (n.node_type === "divergence_alert") {
      items.push({
        id: n.node_id,
        kind: "alert",
        at: n.timestamp,
        severity: "high",
        summary: `Divergence alert · ${a.reasoning ?? n.label}`,
        outcome: a.advisory ? "Advisory — proposal was acknowledged, no external alert" : null,
        node: n,
      });
    }

    if (n.node_type === "documentation") {
      const status = a.approval_status as string | undefined;
      items.push({
        id: n.node_id,
        kind: "documentation",
        at: n.timestamp,
        severity: "low",
        summary: "Operative note ready for review",
        outcome:
          status && status !== "pending"
            ? status === "approved"
              ? "Approved — filed to FHIR"
              : "Rejected — nothing written"
            : null,
        node: n,
      });
    }

    if (n.node_type === "benchmark") {
      const f1 = Number(a.macro_f1 ?? 0);
      const vs = Number(a.vs_cares ?? 0);
      items.push({
        id: n.node_id,
        kind: "benchmark",
        at: n.timestamp,
        // Informational: this is a result to read, not something needing a
        // decision. Its importance is in the number, not in an urgency level.
        severity: "low",
        summary: `Case self-graded · macro-F1 ${f1.toFixed(3)} (${vs >= 0 ? "+" : ""}${vs.toFixed(3)} vs CARES)`,
        outcome: null,
        node: n,
      });
    }
  }

  return items.sort((x, y) => y.at.localeCompare(x.at));
}

/** The single most important current state. Most urgent wins, so a red alert
 *  is never hidden behind a pending proposal. */
function attentionState(items: TimelineItem[]) {
  const unackAlerts = items.filter((i) => i.kind === "alert" && !i.outcome);
  const pendingDocs = items.filter((i) => i.kind === "documentation" && !i.outcome);
  const pendingProposals = items.filter((i) => i.kind === "proposal" && !i.outcome);

  if (unackAlerts.length) return { tone: "high" as const, text: "Divergence alert · unacknowledged", count: unackAlerts.length };
  if (pendingDocs.length) return { tone: "doc" as const, text: "Documentation awaiting review", count: pendingDocs.length };
  if (pendingProposals.length)
    return {
      tone: "moderate" as const,
      text: `${pendingProposals.length} proposal${pendingProposals.length > 1 ? "s" : ""} pending acknowledgment`,
      count: pendingProposals.length,
    };
  return { tone: "neutral" as const, text: "System monitoring · all clear", count: 0 };
}

/** A node reference that jumps the graph to it. */
function GraphLink({ nodeId, children }: { nodeId: string; children: React.ReactNode }) {
  return (
    <button className="aa__graph-link" onClick={() => focusNode(nodeId)} title="Show this node in the graph">
      {children}
    </button>
  );
}

function Citations({
  nodes,
  edges,
  complicationId,
}: {
  nodes: GraphNodePatch[];
  edges: TimelineEdge[];
  complicationId?: string;
}) {
  // Only papers that genuinely SUPPORT the claim — an `evidence` edge pointing
  // at this complication. Retrieval also writes a `hierarchy` edge for every
  // paper it merely consulted, and showing those here would imply support that
  // was never asserted.
  const supportingIds = new Set(
    edges.filter((e) => e.edgeKind === "evidence" && (!complicationId || e.target === complicationId)).map((e) => e.source),
  );
  const cited = nodes.filter((n) => n.node_type === "literature_evidence" && supportingIds.has(n.node_id));
  if (!cited.length) return <div className="aa__empty">No supporting citation — the reasoning was not literature-grounded.</div>;
  return (
    <ul className="aa__citations">
      {cited.slice(0, 3).map((c) => (
        <li key={c.node_id}>
          <GraphLink nodeId={c.node_id}>{c.label}</GraphLink>
          <span className="aa__citation-meta">
            {String(c.attrs?.journal ?? "")} {String(c.attrs?.year ?? "")}
            {c.attrs?.url ? (
              <>
                {" · "}
                <a href={String(c.attrs.url)} target="_blank" rel="noreferrer">
                  source ↗
                </a>
              </>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function AutonomousActionsPanel({ caseId, nodes, edges }: Props) {
  const items = useMemo(() => buildTimeline(nodes), [nodes]);
  const attention = attentionState(items);

  // Only one row open at a time — the panel is too narrow for two.
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [errorFor, setErrorFor] = useState<Record<string, string>>({});

  // Chime once per genuinely new alert, never on re-render or resync.
  const chimed = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const item of items) {
      if (item.kind !== "alert" || chimed.current.has(item.id)) continue;
      const isNew = chimed.current.size > 0; // stay silent on the first paint
      chimed.current.add(item.id);
      if (isNew && !item.outcome) chime();
    }
  }, [items]);

  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.node_id, n])), [nodes]);

  async function run(id: string, fn: () => Promise<unknown>) {
    setBusy(id);
    setErrorFor((e) => ({ ...e, [id]: "" }));
    try {
      await fn();
      // No optimistic update — the SSE stream delivers the real state. Showing
      // it as done before the server agreed would tell a surgeon their action
      // took effect when it might not have.
    } catch (err) {
      setErrorFor((e) => ({ ...e, [id]: err instanceof Error ? err.message : "failed" }));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="tile" data-tile="autonomous-actions">
      <div className="tile__header">
        <h3>Autonomous Actions, Alerts &amp; HITL Approvals</h3>
      </div>

      <div className="tile__body tile__body--column aa">
        <div className={`aa__attention aa__attention--${attention.tone}`}>
          <span>{attention.text}</span>
          {attention.count > 1 && <span className="aa__attention-count">{attention.count}</span>}
        </div>

        {!items.length ? (
          <p className="tile__placeholder">
            {caseId
              ? "Nothing needs attention yet. Proposals, divergence alerts and the operative note appear here as they happen."
              : "Press play to open a case."}
          </p>
        ) : (
          <div className="aa__timeline">
            {items.map((item) => {
              const open = openId === item.id;
              // The collapsed row itself gets a one-click yes/no for whichever
              // items actually have a pending human decision — a surgeon
              // scanning a long timeline shouldn't have to open every proposal
              // just to acknowledge it. Kept to exactly the two kinds that
              // HAVE a decision (proposal, documentation); an alert or a
              // benchmark has nothing here to say yes or no to.
              const quickAccept =
                item.kind === "proposal"
                  ? () => run(item.id, () => acknowledgeProposal(item.node.node_id, "acknowledged"))
                  : item.kind === "documentation"
                    ? () => run(item.id, () => approveDocumentation("approved"))
                    : null;
              const quickReject =
                item.kind === "proposal"
                  ? () => run(item.id, () => acknowledgeProposal(item.node.node_id, "dismissed"))
                  : item.kind === "documentation"
                    ? () => run(item.id, () => approveDocumentation("rejected"))
                    : null;
              const isBusy = busy === item.id;

              return (
                <div key={item.id} className={`aa__item aa__item--${item.severity}${item.outcome ? " aa__item--resolved" : ""}`}>
                  <div className="aa__row-wrap">
                    {/* Not a <button> any more — it now holds real <button>
                        children (the quick tick/cross below), and a button
                        cannot nest a button. role="button" + a key handler
                        keeps it exactly as accessible as it was. */}
                    <div
                      className="aa__row"
                      role="button"
                      tabIndex={0}
                      onClick={() => setOpenId(open ? null : item.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setOpenId(open ? null : item.id);
                        }
                      }}
                    >
                      <span className="aa__row-tags">
                        <span className="aa__time">{timeOf(item.at)}</span>
                        <span className={`aa__tag aa__tag--${item.kind}`}>
                          {KIND_ICON[item.kind]} {KIND_LABEL[item.kind]}
                        </span>
                        <span className={`aa__sev aa__sev--${item.severity}`}>{item.severity}</span>
                        <span className="aa__chevron">{open ? "▾" : "▸"}</span>
                      </span>
                      <span className="aa__summary">
                        {item.summary}
                        {item.outcome && <span className="aa__outcome"> · {item.outcome}</span>}
                      </span>
                    </div>

                    {!item.outcome && quickAccept && quickReject && (
                      <span className="aa__quick-actions">
                        <button
                          className="aa__quick-btn aa__quick-btn--yes"
                          disabled={isBusy}
                          title={item.kind === "documentation" ? "Approve" : "Acknowledge"}
                          aria-label={item.kind === "documentation" ? "Approve" : "Acknowledge"}
                          onClick={(e) => {
                            e.stopPropagation();
                            quickAccept();
                          }}
                        >
                          ✓
                        </button>
                        <button
                          className="aa__quick-btn aa__quick-btn--no"
                          disabled={isBusy}
                          title={item.kind === "documentation" ? "Reject" : "Dismiss"}
                          aria-label={item.kind === "documentation" ? "Reject" : "Dismiss"}
                          onClick={(e) => {
                            e.stopPropagation();
                            quickReject();
                          }}
                        >
                          ✕
                        </button>
                      </span>
                    )}
                  </div>

                  {open && (
                    <div className="aa__detail">
                      {item.kind === "proposal" && (
                        <ProposalDetail
                          item={item}
                          nodeById={nodeById}
                          nodes={nodes}
                          edges={edges}
                          busy={busy === item.id}
                          error={errorFor[item.id]}
                          onAct={(outcome) => run(item.id, () => acknowledgeProposal(item.node.node_id, outcome))}
                        />
                      )}
                      {item.kind === "alert" && <AlertDetail item={item} nodeById={nodeById} nodes={nodes} edges={edges} />}
                      {item.kind === "benchmark" && <BenchmarkDetail item={item} nodes={nodes} edges={edges} />}
                      {item.kind === "documentation" && (
                        <DocumentationDetail
                          item={item}
                          busy={busy === item.id}
                          error={errorFor[item.id]}
                          onAct={(outcome) => run(item.id, () => approveDocumentation(outcome))}
                        />
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function ProposalDetail({
  item,
  nodeById,
  nodes,
  edges,
  busy,
  error,
  onAct,
}: {
  item: TimelineItem;
  nodeById: Map<string, GraphNodePatch>;
  nodes: GraphNodePatch[];
  edges: TimelineEdge[];
  busy: boolean;
  error?: string;
  onAct: (outcome: "acknowledged" | "dismissed") => void;
}) {
  const a = item.node.attrs ?? {};
  const complication = nodeById.get(String(a.complication_id ?? ""));
  const rootError = nodeById.get(String(a.root_error_id ?? ""));
  const ca = complication?.attrs ?? {};

  return (
    <>
      <p className="aa__framing">
        {rootError ? <GraphLink nodeId={rootError.node_id}>{rootError.label}</GraphLink> : "An error"} was detected.
        {complication ? (
          <>
            {" "}Complication considered: <GraphLink nodeId={complication.node_id}>{complication.label}</GraphLink> (
            {String(ca.confidence ?? "?")} confidence, {ca.evidence_backed ? "literature-grounded" : "not evidence-supported"}).
          </>
        ) : null}
      </p>

      <div className="aa__section-label">Proposed actions</div>
      <ul className="aa__steps">
        {((a.steps as { order: number; action: string }[] | undefined) ?? []).map((s) => (
          <li key={s.order}>{s.action}</li>
        ))}
      </ul>

      <div className="aa__section-label">Evidence</div>
      <Citations nodes={nodes} edges={edges} complicationId={complication?.node_id} />

      <div className="aa__provenance">Error Detection → Complication Reasoning → Corrective Replanning</div>
      <div className="aa__provenance">
        Action provenance: tier {String((a.provenance as { tier?: number } | undefined)?.tier ?? "?")} · not reviewed by a practising surgeon
      </div>

      {item.outcome ? (
        <div className="aa__resolved-note">{item.outcome}</div>
      ) : (
        <div className="aa__actions">
          <button className="aa__btn" disabled={busy} onClick={() => onAct("acknowledged")}>
            Acknowledge
          </button>
          <button className="aa__btn" disabled={busy} onClick={() => onAct("dismissed")}>
            Dismiss
          </button>
        </div>
      )}
      {error && <div className="aa__error">{error}</div>}
    </>
  );
}

function AlertDetail({
  item,
  nodeById,
  nodes,
  edges,
}: {
  item: TimelineItem;
  nodeById: Map<string, GraphNodePatch>;
  nodes: GraphNodePatch[];
  edges: TimelineEdge[];
}) {
  const a = item.node.attrs ?? {};
  const proposal = nodeById.get(String(a.proposal_id ?? ""));
  const complication = nodeById.get(String(proposal?.attrs?.complication_id ?? ""));
  const rootError = nodeById.get(String(proposal?.attrs?.root_error_id ?? ""));

  // The gate outcome and the delivery result for this alert.
  const block = nodes.find((n) => n.node_type === "verification_block" && n.attrs?.subject_node_id === item.node.node_id);
  const intent = nodes.find((n) => n.node_type === "action_intent" && n.attrs?.alert_node_id === item.node.node_id);
  const outcome = nodes.find((n) => n.node_type === "action_outcome" && intent && n.node_id.endsWith(intent.node_id));

  return (
    <>
      <p className="aa__framing">
        {String(a.reasoning ?? "")} Detected {String(a.detection_method ?? "")}, confidence {String(a.confidence ?? "?")}.
      </p>

      <div className="aa__section-label">Reasoning trail</div>
      <div className="aa__trail">
        {rootError && <GraphLink nodeId={rootError.node_id}>{rootError.label}</GraphLink>}
        {complication && <> → <GraphLink nodeId={complication.node_id}>{complication.label}</GraphLink></>}
        {proposal && <> → <GraphLink nodeId={proposal.node_id}>{proposal.label}</GraphLink></>}
        {" → "}
        {proposal?.attrs?.acknowledgment_outcome ? String(proposal.attrs.acknowledgment_outcome) : "not acknowledged"}
        {" → "}
        <GraphLink nodeId={item.node.node_id}>divergence observed</GraphLink>
      </div>

      <div className="aa__section-label">Evidence</div>
      <Citations nodes={nodes} edges={edges} complicationId={complication?.node_id} />

      {block && (
        <div className={`aa__verify aa__verify--${block.attrs?.passed ? "pass" : "block"}`}>
          {block.attrs?.passed
            ? `Verification Gate: PASSED at ${timeOf(block.timestamp)}`
            : `Verification Gate: BLOCKED — ${String((block.attrs?.block_reasons as string[] | undefined)?.[0] ?? "")}`}
        </div>
      )}

      {outcome && (
        <div className="aa__autonomous">
          {outcome.label}
          {outcome.attrs?.resource_url ? (
            <>
              {" · "}
              <a href={String(outcome.attrs.resource_url)} target="_blank" rel="noreferrer">
                open record ↗
              </a>
            </>
          ) : null}
        </div>
      )}

      {/* No dismiss on an alert — it persists as historical record. */}
      {item.outcome && <div className="aa__resolved-note">{item.outcome}</div>}
    </>
  );
}

function DocumentationDetail({
  item,
  busy,
  error,
  onAct,
}: {
  item: TimelineItem;
  busy: boolean;
  error?: string;
  onAct: (outcome: "approved" | "rejected") => void;
}) {
  const a = item.node.attrs ?? {};
  const sections = (a.sections ?? {}) as Record<string, unknown>;
  const ordered: [string, string][] = [
    ["Procedure course", "procedure_course"],
    ["Technique observations", "technique_observations"],
    ["Risks considered", "risks_considered"],
    ["Decision support", "decision_support"],
    ["Physiological events", "physiological_events"],
  ];

  return (
    <>
      <p className="aa__framing">{String(sections.summary ?? "")}</p>

      {ordered.map(([title, key]) =>
        sections[key] ? (
          <details className="aa__doc-section" key={key}>
            <summary>{title}</summary>
            <p>{String(sections[key])}</p>
          </details>
        ) : null,
      )}

      {Array.isArray(sections.limitations) && (sections.limitations as string[]).length > 0 && (
        <details className="aa__doc-section" key="limitations">
          <summary>Limitations</summary>
          <ul className="aa__steps">
            {(sections.limitations as string[]).map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </details>
      )}

      {item.outcome ? (
        <div className="aa__resolved-note">{item.outcome}</div>
      ) : (
        <div className="aa__actions">
          <button className="aa__btn aa__btn--primary" disabled={busy} onClick={() => onAct("approved")}>
            Approve &amp; write to FHIR
          </button>
          <button className="aa__btn" disabled={busy} onClick={() => onAct("rejected")}>
            Reject
          </button>
        </div>
      )}
      {error && <div className="aa__error">{error}</div>}
    </>
  );
}


function BenchmarkDetail({
  item,
  nodes,
  edges,
}: {
  item: TimelineItem;
  nodes: GraphNodePatch[];
  edges: TimelineEdge[];
}) {
  const a = item.node.attrs ?? {};
  const counts = (a.category_counts_unscored ?? {}) as Record<string, number>;
  // Which error nodes this scorecard actually graded — the grading edges, so
  // the number traces to what produced it rather than being an assertion.
  const gradedIds = new Set(edges.filter((e) => e.edgeKind === "grading" && e.target === item.node.node_id).map((e) => e.source));
  const graded = nodes.filter((n) => gradedIds.has(n.node_id));

  return (
    <>
      <p className="aa__framing">
        The case graded its own detections against ground truth at close. {String(a.n ?? 0)} windows scored.
      </p>

      <div className="aa__scorecard">
        <div className="aa__score-main">
          <span className="aa__score-value">{Number(a.macro_f1 ?? 0).toFixed(3)}</span>
          <span className="aa__score-label">macro-F1</span>
        </div>
        <div className="aa__score-vs">
          {Number(a.vs_cares ?? 0) >= 0 ? "+" : ""}
          {Number(a.vs_cares ?? 0).toFixed(3)} vs CARES published {String(a.cares_published_macro_f1 ?? "")}
        </div>
      </div>

      <div className="aa__confusion">
        {(["tp", "fp", "fn", "tn"] as const).map((k) => (
          <span key={k}>
            <b>{String(a[k] ?? 0)}</b> {k}
          </span>
        ))}
      </div>

      <div className="aa__section-label">Detections graded</div>
      <div className="aa__trail">
        {graded.length ? (
          graded.slice(0, 6).map((n, i) => (
            <span key={n.node_id}>
              {i > 0 && " · "}
              <GraphLink nodeId={n.node_id}>{n.label}</GraphLink>
            </span>
          ))
        ) : (
          <span className="aa__empty">No error nodes were graded.</span>
        )}
        {graded.length > 6 && <span className="aa__citation-meta"> +{graded.length - 6} more</span>}
      </div>

      {Object.keys(counts).length > 0 && (
        <>
          <div className="aa__section-label">Categories fired — descriptive, not scored</div>
          <div className="aa__trail">
            {Object.entries(counts)
              .map(([k, v]) => `${k.replace(/_/g, " ")} ×${v}`)
              .join(" · ")}
          </div>
        </>
      )}

      {/* Why there is no per-category score. Stated on the surface rather than
          left for someone to wonder about a missing breakdown. */}
      <div className="aa__provenance">{String(a.axis_note ?? "")}</div>
    </>
  );
}
