import { Link } from "react-router-dom";
import { HeroGraph } from "./HeroGraph";

export function Hero() {
  return (
    <header className="home__hero" id="top">
      <div className="home__hero-inner">
        <div>
          <span className="home__badge">✦ All Things Agentic Hackathon • Taskmaster Track</span>
          <h1 className="home__hero-headline">
            <span className="home__hero-headline-accent">Autonomous Surgical Safety System</span> 
          </h1>
          <h2>Improving Patient
            Outcomes and Surgeon Efficiency
          </h2>
          <br></br>

          <div className="home__hero-flow">
            <span className="home__hero-flow-lead">SurgGraph tracks surgery trajectory in real-time</span>
            <ul className="home__hero-flow-list">
              {[
                "Detects Surgical Errors",
                "Reasons possible complications",
                "Recommends corrective measures",
                "Alerts on surgical plan divergence",
                "Writes FHIR Records",
              ].map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ul>
          </div>

          <div className="home__hero-visual-caption">
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              Live Gemini reasoning
            </span>
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              Fail-closed & HITL by design
            </span>
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              FHIR writes
            </span>
          </div>

          <div className="home__hero-ctas">
            <Link to="/console" className="home__btn home__btn--primary">
              ▶ Launch Case
            </Link>
            <a href="#how-it-works" className="home__btn home__btn--secondary">
              See How It Works →
            </a>
          </div>
        </div>

        <HeroGraph />
      </div>
    </header>
  );
}
