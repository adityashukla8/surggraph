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
type ItemKind = "proposal" | "alert" | "documentation" | "benchmark" | "model_armor" | "fhir_write";

/** The category label shown as a pastel tag. Colours follow the graph's own
 *  node-kind palette so an item here and its node there read as the same
 *  thing — yellow for corrective proposals, red for divergence, blue for the
 *  record, brown for the post-case scorecard, green for the content-safety
 *  gate (matching model_armor_screen's own graph outline colour). */
const KIND_LABEL: Record<ItemKind, string> = {
  proposal: "Corrective Proposal",
  alert: "Divergence Alert",
  documentation: "Operative Note",
  benchmark: "Self-Benchmark",
  model_armor: "Model Armor",
  fhir_write: "FHIR Write",
};

interface TimelineItem {
  id: string;
  kind: ItemKind;
  at: string;
  severity: Severity;
  summary: string;
  outcome: string | null; // set once resolved; shown in the collapsed row
  node: GraphNodePatch;
  // The real FHIR resource URL, once the write has actually landed — null
  // until then (proposal/benchmark items never get one). Sourced from the
  // action_outcome node's own resource_url attr, not asserted here.
  resourceUrl: string | null;
  // True only for the documentation item's brief "drafting" state (real
  // backend status, not fabricated) — no sections exist yet to review and
  // no decision is possible, so this is neither pending-a-decision nor
  // resolved. Always false for every other kind.
  drafting: boolean;
}

const KIND_ICON: Record<ItemKind, string> = {
  proposal: "⇢",
  alert: "⚠",
  documentation: "▤",
  benchmark: "▦",
  model_armor: "⛨",
  fhir_write: "▤",
};

/** Real uploaded icon files (ui/frontend/public/icons/) — only some kinds
 *  have one yet. Kinds absent here keep using KIND_ICON's glyph above. */
const KIND_ICON_IMAGE: Partial<Record<ItemKind, string>> = {
  alert: "/icons/divergence-alert.png",
  documentation: "/icons/documentation.png",
  model_armor: "/icons/model-armor.png",
  fhir_write: "/icons/documentation.png",
};

function KindIcon({ kind }: { kind: ItemKind }) {
  const src = KIND_ICON_IMAGE[kind];
  return src ? <img className="aa__kind-icon-img" src={src} alt="" /> : <>{KIND_ICON[kind]}</>;
}

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
        resourceUrl: null, // a proposal is never itself written to FHIR
        drafting: false,
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
        // The real FHIR write now gets its own dedicated timeline item (see
        // the action_outcome branch below) instead of being a link buried
        // inside this row — writing to FHIR is a distinct autonomous action,
        // not a footnote on the alert that triggered it.
        resourceUrl: null,
        drafting: false,
      });
    }

    // The actual external write — real, dedicated item per the underlying
    // AlertDelivery record (tools/fhir_alert.py: "DELIVERY FAILURE IS AN
    // OUTCOME, NOT AN EXCEPTION" — a failed delivery is still a real, visible
    // action_outcome node, never silently absorbed into whatever triggered
    // it). Covers both real sources of a FHIR write: a divergence alert
    // (action_intent.attrs.alert_node_id) and an approved operative note
    // (action_intent.attrs.documentation_node_id) — same real mechanism
    // either way, so one branch handles both rather than duplicating it.
    // A real, independent historical record each time — unlike the old
    // link-lookup-on-the-documentation-item approach, a redraft/re-approval
    // can never make an EARLIER write here look like it belongs to a LATER
    // draft, because each is its own item, not a live lookup.
    if (n.node_type === "action_outcome") {
      const intent = nodes.find((x) => x.node_type === "action_intent" && n.node_id.endsWith(x.node_id));
      if (intent?.attrs?.alert_node_id || intent?.attrs?.documentation_node_id) {
        // Real, explicit backend signals (agents/alert_routing/agent.py::
        // _record_outcome, agents/hitl/approval.py::_write_outcome) — not
        // inferred from resource_url's presence. Three genuinely different
        // states, not two: a verification-gate SUPPRESSION is the fail-
        // closed gate doing its job correctly, not a malfunction, and must
        // not be worded like one — conflating it with a real delivery
        // failure would misdescribe the one case where the system is
        // working exactly as designed.
        const delivered = Boolean(a.delivered);
        const blocked = Boolean(a.blocked);
        const url = a.resource_url;
        items.push({
          id: n.node_id,
          kind: "fhir_write",
          at: n.timestamp,
          // Both a suppression and a genuine failure are worth a surgeon's
          // eye — neither should read as "all clear" — but only a real
          // failure is a malfunction; that distinction lives in `outcome`'s
          // wording, not in severity.
          severity: delivered ? "low" : "high",
          summary: n.label, // backend already picked the exact right phrasing for the real outcome — one source of truth, not a re-derived string
          outcome: delivered ? "Filed" : blocked ? "Suppressed by verification gate" : "Delivery failed",
          node: n,
          resourceUrl: delivered && typeof url === "string" && url.length > 0 ? url : null,
          drafting: false,
        });
      }
    }

    if (n.node_type === "documentation") {
      const status = a.approval_status as string | undefined;
      const closingOut = status === "closing_out";
      // Same non-actionable shape as drafting (no sections yet, nothing to
      // decide) — closing_out is a real, distinct phase (draining in-flight
      // divergence monitoring before drafting even starts) but behaves
      // identically here, so it folds into the same flag rather than a
      // parallel one every downstream check would need to remember too.
      const drafting = status === "drafting" || closingOut;
      const blockedByArmor = status === "blocked";
      items.push({
        id: n.node_id,
        kind: "documentation",
        at: n.timestamp,
        // As serious as a blocked write anywhere else — the whole point of
        // screening BEFORE a surgeon can approve is that it not read as a
        // routine "ready for review" row.
        severity: blockedByArmor ? "high" : "low",
        summary: closingOut
          ? "Wrapping up the case before drafting…"
          : drafting
            ? "Generating operative report…"
            : blockedByArmor
              ? "Operative note blocked by Model Armor"
              : "Operative note ready for review",
        // "blocked" behaves like "pending" here, not like a resolved
        // outcome: Approve is gone, but Reject still is, and a resolved-
        // looking row with no way to act on it would strand the item. The
        // block itself is conveyed by `summary` and `severity` instead.
        outcome:
          drafting || status === "pending" || blockedByArmor
            ? null
            : status === "approved"
              ? "Approved — filed to FHIR"
              : "Rejected — nothing written",
        node: n,
        // The real write itself is now its own dedicated "fhir_write" item
        // (see the action_outcome branch above) rather than a link looked
        // up and attached here.
        resourceUrl: null,
        drafting,
      });
    }

    if (n.node_type === "model_armor_screen") {
      const status = a.status as string | undefined;
      const blocked = status === "blocked";
      const screening = status === "screening";
      items.push({
        id: n.node_id,
        kind: "model_armor",
        at: n.timestamp,
        // A blocked write is as serious as an unacknowledged divergence
        // alert — same escalation the graph node's own colour already makes
        // (see palette.ts's model_armor_screen status override).
        severity: blocked ? "high" : "low",
        summary: "Model Armor screening · operative note",
        // n.label, not a re-derived string here: the backend already picked
        // the exact right phrasing for the moment (draft-time "cleared for
        // review" vs approval-time "cleared to file", or the real block
        // reason) — one source of truth instead of two copies drifting.
        outcome: screening ? null : n.label,
        node: n,
        resourceUrl: null, // the screen itself is never a FHIR write
        drafting: false,
      });
    }

    // Self-benchmarking is disabled as a functional step (agents/orchestrator/
    // agent.py no longer calls benchmark_case), so no "benchmark" node will
    // ever land here — this block is intentionally unreachable rather than
    // deleted, along with KIND_LABEL/KIND_ICON's "benchmark" entries and
    // BenchmarkDetail below, in case it's wanted again later.
    // if (n.node_type === "benchmark") {
    //   const f1 = Number(a.macro_f1 ?? 0);
    //   const vs = Number(a.vs_cares ?? 0);
    //   items.push({
    //     id: n.node_id,
    //     kind: "benchmark",
    //     at: n.timestamp,
    //     // Informational: this is a result to read, not something needing a
    //     // decision. Its importance is in the number, not in an urgency level.
    //     severity: "low",
    //     summary: `Case self-graded · macro-F1 ${f1.toFixed(3)} (${vs >= 0 ? "+" : ""}${vs.toFixed(3)} vs CARES)`,
    //     outcome: null,
    //     node: n,
    //     resourceUrl: null, // a benchmark is never written to FHIR
    //     drafting: false,
    //   });
    // }
  }

  return items.sort((x, y) => y.at.localeCompare(x.at));
}

/** The single most important current state. Most urgent wins, so a red alert
 *  is never hidden behind a pending proposal. */
function attentionState(items: TimelineItem[]) {
  const unackAlerts = items.filter((i) => i.kind === "alert" && !i.outcome);
  const blockedArmor = items.filter((i) => i.kind === "model_armor" && i.node.attrs?.status === "blocked");
  const failedFhirWrites = items.filter((i) => i.kind === "fhir_write" && i.severity === "high");
  const pendingDocs = items.filter((i) => i.kind === "documentation" && !i.outcome && !i.drafting);
  const pendingProposals = items.filter((i) => i.kind === "proposal" && !i.outcome);
  const draftingDocs = items.filter((i) => i.kind === "documentation" && i.drafting);

  if (unackAlerts.length) return { tone: "high" as const, text: "Divergence alert · unacknowledged", count: unackAlerts.length };
  // A blocked write is as serious as an unacknowledged alert — the whole
  // point of a second fail-closed gate is that it not read as "all clear".
  if (blockedArmor.length) return { tone: "high" as const, text: "Model Armor blocked a write", count: blockedArmor.length };
  // A failed real-world delivery must never quietly blend into "all clear" —
  // tools/fhir_alert.py's own principle: "delivery failure is an outcome,
  // not an exception."
  if (failedFhirWrites.length) return { tone: "high" as const, text: "FHIR write needs attention", count: failedFhirWrites.length };
  if (pendingDocs.length) return { tone: "doc" as const, text: "Documentation awaiting review", count: pendingDocs.length };
  if (pendingProposals.length)
    return {
      tone: "moderate" as const,
      text: `${pendingProposals.length} proposal${pendingProposals.length > 1 ? "s" : ""} pending acknowledgment`,
      count: pendingProposals.length,
    };
  // Genuinely in progress (both the closing_out and drafting statuses are
  // real status writes, see agents/orchestrator/agent.py and agents/
  // documentation/agent.py), not a fake "still working" spinner — the whole
  // ~1-2 minute close-out otherwise leaves the banner reading "all clear"
  // while the system is actually still busy, which is exactly what made it
  // look like nothing was happening after the last divergence alert.
  if (draftingDocs.length) {
    const closingOut = draftingDocs.some((i) => i.node.attrs?.approval_status === "closing_out");
    return { tone: "doc" as const, text: closingOut ? "Wrapping up the case…" : "Preparing operative report…", count: 0 };
  }
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

/** A prominent link at the very top of a detail body, jumping to THIS item's
 *  own node — distinct from GraphLink's inline in-sentence use for OTHER
 *  nodes referenced within the body (a cited root error, a proposal, etc). */
function ShowNodeInGraph({ nodeId }: { nodeId: string }) {
  return (
    <div className="aa__show-in-graph">
      <GraphLink nodeId={nodeId}>↗ Show node in graph</GraphLink>
    </div>
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
                  source <img className="aa__kind-icon-img" src="/icons/literature-evidence.png" alt="" />
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
              // A Model Armor block is real and not the surgeon's to override
              // from this row — Approve disappears, but Reject still lets
              // them close the item out rather than leaving it stuck forever.
              const blockedByArmor = item.kind === "documentation" && item.node.attrs?.approval_status === "blocked";
              const quickAccept =
                item.kind === "proposal"
                  ? () => run(item.id, () => acknowledgeProposal(item.node.node_id, "acknowledged"))
                  : item.kind === "documentation" && !item.drafting && !blockedByArmor
                    ? () => run(item.id, () => approveDocumentation("approved"))
                    : null;
              const quickReject =
                item.kind === "proposal"
                  ? () => run(item.id, () => acknowledgeProposal(item.node.node_id, "dismissed"))
                  : item.kind === "documentation" && !item.drafting
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
                          <KindIcon kind={item.kind} /> {KIND_LABEL[item.kind]}
                        </span>
                        <span className={`aa__sev aa__sev--${item.severity}`}>{item.severity}</span>
                        <span className="aa__chevron">{open ? "▾" : "▸"}</span>
                      </span>
                      <span className="aa__summary">
                        {item.summary}
                        {item.outcome && <span className="aa__outcome"> · {item.outcome}</span>}
                      </span>
                    </div>

                    {/* Independent, not a joint condition: a Model Armor
                        block removes Approve but must not also swallow
                        Reject along with it. */}
                    {!item.outcome && (quickAccept || quickReject) && (
                      <span className="aa__quick-actions">
                        {quickAccept && (
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
                        )}
                        {quickReject && (
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
                        )}
                      </span>
                    )}

                    {/* Only reachable once resolved (resourceUrl only exists
                        after a real FHIR write lands) — never overlaps with
                        the tick/cross above, which only show pre-resolution. */}
                    {item.resourceUrl && (
                      <span className="aa__quick-actions">
                        <a
                          className="aa__quick-btn aa__quick-link"
                          href={item.resourceUrl}
                          target="_blank"
                          rel="noreferrer"
                          title="Open the real FHIR record"
                          aria-label="Open the real FHIR record"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <img className="aa__kind-icon-img" src="/icons/documentation.png" alt="" />
                        </a>
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
                      {item.kind === "fhir_write" && <FhirWriteDetail item={item} nodes={nodes} />}
                      {item.kind === "model_armor" && <ModelArmorDetail item={item} />}
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

  // The gate outcome for this alert. The delivery result itself is now its
  // own dedicated timeline item (kind "fhir_write") rather than shown here.
  const block = nodes.find((n) => n.node_type === "verification_block" && n.attrs?.subject_node_id === item.node.node_id);

  return (
    <>
      <ShowNodeInGraph nodeId={item.node.node_id} />
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

  const blockedByArmor = a.approval_status === "blocked";

  if (item.drafting) {
    // Nothing to review yet — the Gemini call that produces `sections` is
    // still in flight (or hasn't started: closing_out is the earlier phase
    // draining in-flight divergence monitoring first). Showing the (empty)
    // section list or the approve/reject buttons here would invite a click
    // that has nothing real to act on.
    const closingOut = a.approval_status === "closing_out";
    return (
      <>
        <ShowNodeInGraph nodeId={item.node.node_id} />
        <p className="aa__framing">
          {closingOut
            ? "Letting in-flight divergence monitoring finish before drafting starts — this usually takes under a minute."
            : "Drafting from the case's full reasoning graph — this usually takes 1-2 minutes."}
        </p>
      </>
    );
  }

  return (
    <>
      <ShowNodeInGraph nodeId={item.node.node_id} />
      {blockedByArmor && (
        <div className="aa__verify aa__verify--block">
          Blocked by Model Armor — {String(a.model_armor_reason ?? "flagged content")}. The draft below is shown for
          review, but cannot be approved as written.
        </div>
      )}
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
        // The real write itself (if approval led to one) is its own
        // dedicated "fhir_write" timeline item now, not a link here.
        <div className="aa__resolved-note">{item.outcome}</div>
      ) : (
        <div className="aa__actions">
          {!blockedByArmor && (
            <button className="aa__btn aa__btn--primary" disabled={busy} onClick={() => onAct("approved")}>
              Approve &amp; write to FHIR
            </button>
          )}
          <button className="aa__btn" disabled={busy} onClick={() => onAct("rejected")}>
            Reject
          </button>
        </div>
      )}
      {error && <div className="aa__error">{error}</div>}
    </>
  );
}

/** The content-safety gate on the operative note's outbound FHIR write —
 *  agents/hitl/approval.py::_file_record, tools/model_armor.py. Nothing to
 *  decide here (fully automated, not a HITL item), so no actions — just what
 *  it found and where it happened. */
function ModelArmorDetail({ item }: { item: TimelineItem }) {
  const a = item.node.attrs ?? {};
  const status = a.status as string | undefined;
  const matchState = a.raw_filter_match_state as string | undefined;

  return (
    <>
      <ShowNodeInGraph nodeId={item.node.node_id} />
      <p className="aa__framing">
        {status === "screening"
          ? "Screening this note's real text for injected or sensitive content before it can be filed."
          : status === "blocked"
            ? `Blocked: ${String(a.reason ?? "Model Armor flagged this content")}`
            : "Cleared — no injected, malicious, or sensitive content detected."}
      </p>
      {matchState && <div className="aa__provenance">Model Armor verdict: {matchState}</div>}
      {item.outcome && <div className={`aa__verify aa__verify--${status === "blocked" ? "block" : "pass"}`}>{item.outcome}</div>}
    </>
  );
}

/** The real external write itself — a distinct autonomous action from the
 *  alert that triggered it (tools/fhir_alert.py), so it gets its own
 *  dedicated item rather than a link folded into that alert's row. */
function FhirWriteDetail({ item, nodes }: { item: TimelineItem; nodes: GraphNodePatch[] }) {
  const intent = nodes.find((n) => n.node_type === "action_intent" && item.node.node_id.endsWith(n.node_id));
  // Either real source of a write: a divergence alert, or an approved
  // operative note — whichever attr the intent actually carries.
  const sourceId = intent?.attrs?.alert_node_id ?? intent?.attrs?.documentation_node_id;
  const source = nodes.find((n) => n.node_id === sourceId);

  return (
    <>
      <ShowNodeInGraph nodeId={item.node.node_id} />
      <p className="aa__framing">{item.node.label}</p>
      {source && (
        <div className="aa__trail">
          Delivered for: <GraphLink nodeId={source.node_id}>{source.label}</GraphLink>
        </div>
      )}
      {item.resourceUrl ? (
        <div className="aa__autonomous">
          <a href={item.resourceUrl} target="_blank" rel="noreferrer">
            open record <img className="aa__kind-icon-img" src="/icons/documentation.png" alt="" />
          </a>
        </div>
      ) : (
        <div className="aa__verify aa__verify--block">{item.outcome}</div>
      )}
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
