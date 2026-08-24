import { FastApiLogo, ReactLogo, XyFlowLogo, FhirLogo } from "./TechLogos";

const TECH = [
  { logo: <img src="/tech-logos/gemin.png" alt="Gemini" />, name: "Gemini 3.5", desc: "Vertex AI · global endpoint, every reasoning call" },
  { logo: <img src="/tech-logos/adk.png" alt="Google ADK" />, name: "Google ADK", desc: "Multi-agent orchestration, event-driven + sweep agents" },
  { logo: <img src="/tech-logos/modelarmor.png" alt="Model Armor" />, name: "Model Armor", desc: "Content-safety gate on the outbound FHIR write" },
  { logo: <img src="/tech-logos/cloudrun.png" alt="Cloud Run" />, name: "Cloud Run", desc: "State service, orchestrator service, this frontend" },
  { logo: <img src="/tech-logos/firestore.png" alt="Firestore" />, name: "Firestore", desc: "Multi-tenant, per-case-isolated Living State Graph" },
  { logo: <FastApiLogo />, name: "FastAPI + SSE", desc: "Streams every graph patch live, no polling" },
  { logo: <FhirLogo />, name: "HAPI FHIR", desc: "Real DocumentReference + Communication writes" },
  {
    logo: (
      <span style={{ display: "inline-flex", gap: 4 }}>
        <ReactLogo size={20} />
        <XyFlowLogo size={20} />
      </span>
    ),
    name: "React + ReactFlow",
    desc: "This site and the live console at /console",
  },
];

export function TechSection() {
  return (
    <section className="home__section" id="tech">
      <span className="home__eyebrow">Tech Stack</span>
      <h2 className="home__headline" style={{ fontSize: 32, marginBottom: 40 }}>
        Built on real Google Cloud infrastructure, not a demo shim
      </h2>
      <div className="home__tech-grid">
        {TECH.map((t) => (
          <div className="home__agent-card" key={t.name} style={{ ["--agent-color" as string]: "var(--home-accent)" }}>
            <div className="home__card-icon" style={{ ["--card-color" as string]: "var(--home-accent)", marginBottom: 12 }}>
              {t.logo}
            </div>
            <h4 className="home__agent-name" style={{ marginBottom: 6 }}>{t.name}</h4>
            <p className="home__agent-desc">{t.desc}</p>
          </div>
        ))}
      </div>
    </section>
  );
}
