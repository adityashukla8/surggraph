import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphNodePatch } from "../../graph/types";

// Case Context — the ambient "who is on the table and what is their body
// doing" surface. Mostly static (the patient twin), one live region (vitals).
//
// Deliberately quiet. This panel sits beside the graph and the alerts panel,
// both of which legitimately demand attention; this one should read as
// background. Neutral colours, low motion, no animation on the numbers.
//
// EVERYTHING COMES FROM THE GRAPH. The twin is read off the patient_twin node
// and the vitals off the snapshot slot, both delivered by the same SSE stream
// that drives the graph. The panel holds no copy of the patient and computes
// nothing clinical — D'Amico risk, which fields count as elevated priors, and
// the risk summary are all derived once in tools/patient_twin.py and carried
// on the node, so this panel and the reasoning prompts cannot disagree.

interface CaseContextPanelProps {
  caseId: string | null;
  nodes: GraphNodePatch[];
}

// How much history the sparklines show. The vitals slot updates roughly once
// per perception window (~5s), so five minutes is on the order of 60 points.
const SPARKLINE_WINDOW_MS = 5 * 60 * 1000;

type Vitals = {
  hr_bpm: number;
  map_mmhg: number;
  spo2_pct: number;
  etco2_mmhg: number;
  peak_airway_pressure_cmh2o: number;
  deviations: string[];
  excursion_label: string | null;
  t_s: number;
};

const VITAL_ROWS: { key: keyof Vitals; label: string; unit: string }[] = [
  { key: "hr_bpm", label: "HR", unit: "bpm" },
  { key: "map_mmhg", label: "MAP", unit: "mmHg" },
  { key: "spo2_pct", label: "SpO₂", unit: "%" },
  { key: "etco2_mmhg", label: "EtCO₂", unit: "mmHg" },
  { key: "peak_airway_pressure_cmh2o", label: "PIP", unit: "cmH₂O" },
];

function get(obj: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((cur, key) => (cur && typeof cur === "object" ? (cur as Record<string, unknown>)[key] : undefined), obj);
}

function fmt(value: unknown): string {
  if (value === true) return "Yes";
  if (value === false) return "No";
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.replace(/_/g, " ");
  return String(value);
}

/** A field row, with the amber dot when the reasoning layer treats it as an
 *  elevated prior. The dot and its tooltip come from the profile's own
 *  declaration, never from a rule written here. */
function Field({
  label,
  value,
  suffix,
  priorReason,
}: {
  label: string;
  value: unknown;
  suffix?: string;
  priorReason?: string;
}) {
  return (
    <div className="case-context__field">
      <span className="case-context__field-label">{label}</span>
      <span className="case-context__field-value">
        {fmt(value)}
        {suffix ? ` ${suffix}` : ""}
        {priorReason && (
          <span
            className="case-context__prior-dot"
            title={`Elevated technical difficulty prior — considered by Complication Reasoning.\n\n${priorReason}`}
          >
            ●
          </span>
        )}
      </span>
    </div>
  );
}

function Group({ title, summary, children }: { title: string; summary: string; children: React.ReactNode }) {
  // Native <details>, collapsed by default — the header line already carries
  // the summary a reader needs at a glance, so the panel stays short and the
  // detail is one click away. Uncontrolled on purpose: the browser owns the
  // toggle rather than React re-collapsing it on every SSE re-render.
  return (
    <details className="case-context__group">
      <summary className="case-context__group-header">
        <span>{title}</span>
        <span className="case-context__group-summary">{summary}</span>
      </summary>
      <div className="case-context__group-body">{children}</div>
    </details>
  );
}

/** Inline sparkline. Plain SVG polyline — no chart library for a 60px trace. */
function Sparkline({ points, state }: { points: number[]; state: "normal" | "deviated" }) {
  if (points.length < 2) {
    // An honest empty state. A flat line here would imply a stable vital when
    // what we actually have is not enough history to draw one.
    return <span className="case-context__sparkline-empty">—</span>;
  }
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const w = 60;
  const h = 20;
  const d = points
    .map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p - min) / range) * (h - 2) - 1}`)
    .join(" ");
  return (
    <svg className="case-context__sparkline" width={w} height={h} aria-hidden>
      <polyline points={d} fill="none" strokeWidth={1.25} className={`case-context__spark--${state}`} />
    </svg>
  );
}

export function CaseContextPanel({ caseId, nodes }: CaseContextPanelProps) {
  const twinNode = nodes.find((n) => n.node_type === "patient_twin");
  const vitalsNode = nodes.find((n) => n.node_id === "snapshot:current_vitals_summary");

  const profile = twinNode?.attrs?.profile as Record<string, unknown> | undefined;
  const priors = (twinNode?.attrs?.elevated_prior_fields ?? {}) as Record<string, string>;
  const vitals = vitalsNode?.attrs as unknown as Vitals | undefined;

  // Sparkline history, buffered client-side. The graph carries only the
  // CURRENT vitals slot — it is a fixed-cardinality snapshot by design, so
  // history has to accumulate here rather than being read back.
  const history = useRef<{ at: number; v: Vitals }[]>([]);
  const [, forceRender] = useState(0);
  useEffect(() => {
    if (!vitals) return;
    const last = history.current[history.current.length - 1];
    if (last && last.v.t_s === vitals.t_s) return; // same sample, not a new reading
    const now = Date.now();
    history.current = [...history.current, { at: now, v: vitals }].filter((p) => now - p.at <= SPARKLINE_WINDOW_MS);
    forceRender((n) => n + 1);
  }, [vitals]);

  // Case elapsed time, from the trigger node's own timestamp rather than from
  // when this component mounted — reloading the page must not reset the clock.
  const triggerNode = nodes.find((n) => n.node_type === "trigger");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const elapsed = useMemo(() => {
    if (!triggerNode) return null;
    const started = new Date(triggerNode.timestamp).getTime();
    const secs = Math.max(0, Math.floor((now - started) / 1000));
    return `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
  }, [triggerNode, now]);

  return (
    <div className="tile" data-tile="case-context">
      <div className="tile__header">
        <h3>Case Context</h3>
        <span className="tile__subtitle">Patient twin + vitals · synthetic</span>
      </div>

      <div className="tile__body tile__body--column case-context">
        {/* Sticky, never scrolls away. A viewer must not be able to scroll past
            the disclosure and then read the values as a real record. */}
        <div className="case-context__synthetic-banner">
          SYNTHETIC — illustrative patient profile &amp; simulated vitals
        </div>

        {!profile ? (
          <p className="tile__placeholder">
            {caseId ? "Waiting for the patient profile to load…" : "Press play to open a case."}
          </p>
        ) : (
          <>
            <div className="case-context__identity">
              <span>{caseId ? caseId.replace(/^case-/, "").slice(0, 8) : "—"}</span>
              <span>
                {String(get(profile, "demographics.age_years"))} · {fmt(get(profile, "demographics.sex"))}
              </span>
              <span className="case-context__elapsed">{elapsed ?? "--:--"}</span>
            </div>

            <Group
              title="Demographics & baseline"
              summary={`${get(profile, "demographics.age_years")}y · BMI ${get(profile, "demographics.bmi")} · ASA ${get(profile, "anesthetic_risk.asa_class")}`}
            >
              <Field label="Age" value={get(profile, "demographics.age_years")} suffix="y" />
              <Field label="BMI" value={get(profile, "demographics.bmi")} priorReason={priors["demographics.bmi"]} />
              <Field label="ASA class" value={get(profile, "anesthetic_risk.asa_class")} priorReason={priors["anesthetic_risk.asa_class"]} />
            </Group>

            <Group
              title="Cancer profile"
              summary={`PSA ${get(profile, "prostate.psa_ng_ml")} · Gleason ${get(profile, "prostate.gleason_score")} · ${fmt(twinNode?.attrs?.damico_risk)}`}
            >
              <Field label="PSA" value={get(profile, "prostate.psa_ng_ml")} suffix="ng/mL" />
              <Field label="Biopsy Gleason" value={get(profile, "prostate.gleason_score")} />
              <Field label="D'Amico risk" value={twinNode?.attrs?.damico_risk} />
              <Field label="Clinical T-stage" value={get(profile, "prostate.clinical_stage")} />
            </Group>

            <Group
              title="Anatomy (imaging)"
              summary={`${get(profile, "prostate.volume_ml")} mL${get(profile, "prostate.median_lobe") ? " · median lobe" : ""}`}
            >
              <Field label="Prostate volume" value={get(profile, "prostate.volume_ml")} suffix="mL" priorReason={priors["prostate.volume_ml"]} />
              <Field label="Median lobe" value={get(profile, "prostate.median_lobe")} priorReason={priors["prostate.median_lobe"]} />
              <Field
                label="Membranous urethra"
                value={get(profile, "prostate.membranous_urethra_length_mm")}
                suffix="mm"
                priorReason={priors["prostate.membranous_urethra_length_mm"]}
              />
            </Group>

            <Group title="Surgical plan" summary={fmt(get(profile, "surgical_plan.nerve_sparing"))}>
              <Field label="Nerve-sparing" value={get(profile, "surgical_plan.nerve_sparing")} />
              <Field label="Prior TURP" value={get(profile, "surgical_plan.prior_turp")} priorReason={priors["surgical_plan.prior_turp"]} />
              <Field label="Prior pelvic surgery" value={get(profile, "surgical_plan.prior_pelvic_surgery")} />
              <Field label="Prior radiation" value={get(profile, "surgical_plan.prior_radiation")} />
            </Group>

            <Group
              title="Baseline function"
              summary={`${fmt(get(profile, "baseline_function.preop_continence"))} · IIEF-5 ${get(profile, "baseline_function.iief5_score")}`}
            >
              <Field label="Pre-op continence" value={get(profile, "baseline_function.preop_continence")} />
              <Field label="IIEF-5" value={get(profile, "baseline_function.iief5_score")} />
            </Group>

            <div className="case-context__risk">
              <div className="case-context__risk-header">Risk profile</div>
              <p>{String(twinNode?.attrs?.risk_profile_summary ?? "")}</p>
            </div>

            <div className="case-context__vitals">
              <div className="case-context__group-header case-context__vitals-header">
                <span>Vitals</span>
                {vitals?.excursion_label && <span className="case-context__excursion">{vitals.excursion_label}</span>}
              </div>

              {!vitals ? (
                <p className="tile__placeholder">No vitals yet — the stream starts with the first perception window.</p>
              ) : (
                VITAL_ROWS.map(({ key, label, unit }) => {
                  const value = vitals[key] as number;
                  // Deviation is decided by the vitals generator against the
                  // EXPECTED course for this point in the case, not by a
                  // threshold guessed here — insufflation legitimately shifts
                  // several of these for the whole case.
                  const deviated = (vitals.deviations ?? []).includes(key as string);
                  const points = history.current.map((p) => p.v[key] as number);
                  const prev = points.length >= 2 ? points[points.length - 2] : value;
                  const delta = value - prev;
                  const arrow = Math.abs(delta) < 0.5 ? "→" : delta > 0 ? "↑" : "↓";

                  return (
                    <div key={key} className={`case-context__vital${deviated ? " case-context__vital--deviated" : ""}`}>
                      <span className="case-context__vital-label">{label}</span>
                      <span className="case-context__vital-value">
                        {/* A dot prefix as well as colour, so the deviated
                            state survives a colourblind viewer or a
                            greyscale screenshot. */}
                        {deviated && <span className="case-context__vital-dot">●</span>}
                        {Math.round(value)}
                      </span>
                      <span className="case-context__vital-unit">{unit}</span>
                      <Sparkline points={points} state={deviated ? "deviated" : "normal"} />
                      <span className="case-context__vital-trend">{arrow}</span>
                    </div>
                  );
                })
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
