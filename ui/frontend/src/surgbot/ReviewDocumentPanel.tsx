import { useEffect, useState } from "react";
import type { ApprovalOutcome, ReviewDocument } from "./types";

const SURGBOT_SERVICE_URL = import.meta.env.VITE_SURGBOT_SERVICE_URL ?? "http://127.0.0.1:8091";

// Phase 5's approve/edit/reject UI.
//
// There is no existing edit-UI pattern in this codebase to copy: the
// existing HITL approval flow (AutonomousActionsPanel.tsx's
// DocumentationDetail) only ever renders Approve/Reject buttons, even though
// its own backend (agents/hitl/approval.py, via api/hitl.ts's
// approveDocumentation) already accepts an "edited" outcome + edited_sections
// — that capability has simply never had a frontend. This component is that
// frontend, built from scratch, scoped to SurgBot's own review documents.
// It borrows `.aa__doc-section`/`.aa__btn`'s visual conventions from App.css
// only as a read-only reference (padding, font sizes, border treatment) —
// nothing here imports or edits that file; every class is a new `.sb__*`
// one defined in surgbot.css.

interface Props {
  reviewDocument: ReviewDocument;
}

type Mode = "view" | "edit";

const RESULT_LABEL: Record<ApprovalOutcome, string> = {
  approved: "Approved",
  rejected: "Rejected — nothing carried forward",
  edited: "Saved with edits and approved",
};

function formatSectionLabel(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function ReviewDocumentPanel({ reviewDocument }: Props) {
  const [mode, setMode] = useState<Mode>("view");
  const [draft, setDraft] = useState<Record<string, string>>(reviewDocument.sections);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ApprovalOutcome | null>(null);

  // A fresh review_id (a new Phase-6 draft, e.g. after a reject-and-resynthesize)
  // resets local edit/result state — never carries a stale "approved" banner
  // or a stale draft onto a document the surgeon hasn't seen yet.
  useEffect(() => {
    setDraft(reviewDocument.sections);
    setMode("view");
    setResult(null);
    setError(null);
  }, [reviewDocument.review_id, reviewDocument.sections]);

  async function submit(outcome: ApprovalOutcome) {
    setBusy(true);
    setError(null);
    try {
      const resp = await fetch(`${SURGBOT_SERVICE_URL}/surgbot/reviews/${reviewDocument.review_id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          outcome,
          edited_sections: outcome === "edited" ? draft : undefined,
        }),
      });
      if (!resp.ok) {
        const detail = await resp.text().catch(() => "");
        throw new Error(`${resp.status}${detail ? `: ${detail.slice(0, 160)}` : ""}`);
      }
      setResult(outcome);
      setMode("view");
    } catch (err) {
      // No optimistic success — a control that reports done while nothing
      // was actually filed is worse than a visible error (same principle
      // AutonomousActionsPanel.tsx's `run()` already follows for HITL #1/#2).
      setError(err instanceof Error ? err.message : "Failed to submit the review decision");
    } finally {
      setBusy(false);
    }
  }

  const sectionEntries = Object.entries(reviewDocument.sections);

  return (
    <section className="sb__review" aria-label="Case review document">
      <div className="sb__review-header">
        <h4>Case Review Document</h4>
        <span className={`sb__review-status sb__review-status--${result ?? reviewDocument.approval_status}`}>
          {result ? RESULT_LABEL[result] : reviewDocument.approval_status}
        </span>
      </div>

      {sectionEntries.length === 0 ? (
        <p className="sb__review-empty">Drafting — no sections yet.</p>
      ) : (
        <div className="sb__review-sections">
          {sectionEntries.map(([key, originalValue]) => (
            <div className="sb__review-section" key={key}>
              <div className="sb__review-section-label">{formatSectionLabel(key)}</div>
              {mode === "edit" ? (
                <textarea
                  className="sb__review-textarea"
                  value={draft[key] ?? ""}
                  onChange={(e) => setDraft((prev) => ({ ...prev, [key]: e.target.value }))}
                  rows={4}
                  aria-label={`Edit ${formatSectionLabel(key)}`}
                />
              ) : (
                <p className="sb__review-text">{draft[key] ?? originalValue}</p>
              )}
            </div>
          ))}
        </div>
      )}

      {result ? (
        <div className="sb__review-resolved">{RESULT_LABEL[result]}</div>
      ) : (
        <div className="sb__review-actions">
          {mode === "edit" ? (
            <>
              <button className="sb__btn sb__btn--primary" disabled={busy} onClick={() => submit("edited")}>
                Save &amp; Approve
              </button>
              <button
                className="sb__btn"
                disabled={busy}
                onClick={() => {
                  setMode("view");
                  setDraft(reviewDocument.sections);
                }}
              >
                Cancel edit
              </button>
            </>
          ) : (
            <>
              <button className="sb__btn sb__btn--primary" disabled={busy} onClick={() => submit("approved")}>
                Approve
              </button>
              <button className="sb__btn" disabled={busy} onClick={() => setMode("edit")}>
                Edit
              </button>
              <button className="sb__btn sb__btn--danger" disabled={busy} onClick={() => submit("rejected")}>
                Reject
              </button>
            </>
          )}
        </div>
      )}
      {error && <div className="sb__review-error">{error}</div>}
    </section>
  );
}
