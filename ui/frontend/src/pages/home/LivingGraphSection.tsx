const STATS = [
  { value: "20", label: "node types" },
  { value: "13", label: "relationships" },
  { value: "Live", label: "SSE updates" },
  { value: "1:1", label: "per-case isolation" },
];

export function LivingGraphSection() {
  return (
    <section className="home__section" id="state">
      <div className="home__living-graph">
        <div>
          <span className="home__eyebrow">The Living Graph - Powered by Firestore</span>
          <h2 className="home__headline">Real-time surgical context layer for all agents</h2>
          <p className="home__living-graph-body">
            Every activity, entity, interaction, error, complication, corrective proposal, alert, and verification
            block is a node on one shared graph — updated live over Server-Sent Events, queried by tools rather than
            carried in prompt context, and fully isolated per case.
          </p>
          <div className="home__stat-row">
            {STATS.map((s) => (
              <span className="home__stat" key={s.label}>
                <b>{s.value}</b> {s.label}
              </span>
            ))}
          </div>
        </div>
        <div className="home__living-graph-mini" aria-hidden="true">
          {[
            { label: "◇ Trigger", x: 8, y: 44, color: "#59c3e6" },
            { label: "▲ Error", x: 34, y: 14, color: "#eb6834" },
            { label: "◈ Complication", x: 34, y: 70, color: "#eb6834" },
            { label: "⚠ Divergence", x: 62, y: 40, color: "#e34948" },
            { label: "✦ Model Armor", x: 62, y: 80, color: "#1baf7a" },
            { label: "✦ FHIR write", x: 76, y: 55, color: "#59c3e6" },
          ].map((n) => (
            <div
              key={n.label}
              className="home__graph-node home__graph-node--visible"
              style={{ left: `${n.x}%`, top: `${n.y}%`, ["--node-color" as string]: n.color }}
            >
              <span className="home__graph-node-dot" />
              {n.label}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
