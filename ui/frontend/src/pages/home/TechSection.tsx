import { FastApiLogo, ReactLogo, XyFlowLogo, FhirLogo } from "./TechLogos";

interface TechItem {
  logo: React.ReactNode;
  name: string;
  desc: string;
  wideIcon?: boolean;
}

interface TechCategory {
  label: string;
  items: TechItem[];
}

const CATEGORIES: TechCategory[] = [
  {
    label: "AI Models",
    items: [
      { logo: <img src="/tech-logos/gemin.png" alt="Gemini" />, name: "Gemini 3.5", desc: "Vertex AI · global endpoint, every reasoning call, including clinical documentation" },
      {
        logo: <img src="/tech-logos/gemin.png" alt="MedASR" />,
        name: "MedASR",
        desc: "Self-deployed Vertex AI Conformer ASR - real-time speech-to-text for SurgBot voice sessions",
      },
      {
        logo: <img src="/tech-logos/gemin.png" alt="Chirp" />,
        name: "Chirp 3 HD",
        desc: "Cloud Text-to-Speech - synthesizes SurgBot's spoken replies",
      },
    ],
  },
  {
    label: "Agent Platform",
    items: [
      { logo: <img src="/tech-logos/vertexai.png" alt="Vertex AI" />, name: "Vertex AI", desc: "Model serving & endpoints - hosts every Gemini call" },
      {
        logo: <img src="/tech-logos/gemin.png" alt="Gemini" />,
        name: "Gemini Enterprise Agent Platform",
        desc: "Agent Runtime + Registry hosting SurgBot's root agent and 3 subagents",
      },
      { logo: <img src="/tech-logos/modelarmor.png" alt="Model Armor" />, name: "Model Armor", desc: "Content-safety gate on the outbound FHIR write" },
    ],
  },
  {
    label: "Cloud Infrastructure",
    items: [
      { logo: <img src="/tech-logos/cloudrun.png" alt="Cloud Run" />, name: "Cloud Run", desc: "State service, orchestrator service, Surgbot service, this frontend" },
      { logo: <img src="/tech-logos/cloudbuild.png" alt="Cloud Build" />, name: "Cloud Build", desc: "CI/CD - builds and deploys every Cloud Run service" },
      { logo: <img src="/tech-logos/firestore.png" alt="Firestore" />, name: "Firestore", desc: "Multi-tenant, per-case-isolated Living State Graph" },
    ],
  },
  {
    label: "Application Layer",
    items: [
      { logo: <FhirLogo />, name: "HAPI FHIR", desc: "Real DocumentReference + Communication writes" },
      { logo: <FastApiLogo />, name: "FastAPI + SSE", desc: "Streams every graph patch live, no polling" },
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
    ],
  },
];

export function TechSection() {
  return (
    <section className="home__section" id="tech">
      <span className="home__eyebrow">Tech Stack</span>
      <h2 className="home__headline" style={{ fontSize: 32, marginBottom: 40 }}>
        Built Google Cloud infrastructure
      </h2>
      {CATEGORIES.map((cat) => (
        <div className="home__tech-category" key={cat.label}>
          <span className="home__tech-category-label">{cat.label}</span>
          <div className="home__tech-grid">
            {cat.items.map((t) => (
              <div className="home__agent-card" key={t.name} style={{ ["--agent-color" as string]: "var(--home-accent)" }}>
                <div
                  className="home__card-icon"
                  style={{
                    ["--card-color" as string]: "var(--home-accent)",
                    marginBottom: 12,
                    ...(t.wideIcon ? { width: "auto", minWidth: 38, padding: "0 8px" } : {}),
                  }}
                >
                  {t.logo}
                </div>
                <h4 className="home__agent-name" style={{ marginBottom: 6 }}>{t.name}</h4>
                <p className="home__agent-desc">{t.desc}</p>
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}
