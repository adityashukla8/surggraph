import { Link } from "react-router-dom";
import { HeroGraph } from "./HeroGraph";

export function Hero() {
  return (
    <header className="home__hero" id="top">
      <div className="home__hero-inner">
        <div>
          <span className="home__badge">✦ All Things Agentic Hackathon • Taskmaster Track</span>
          <h1 className="home__hero-headline">
            <span className="home__hero-headline-accent">Autonomous, Continuously Improving Surgical Safety System</span> 
          </h1>
          <h2>Improving Patient
            Outcomes and Surgeon Efficiency by Detecting Surgical Errors & Complications
          </h2>
          <br></br>

          <div className="home__hero-flow">
            <span className="home__hero-flow-lead">SurgOS runs a surgical case as a continuously improving, closed loop, 2-fold system:</span>
            <ul className="home__hero-flow-list">
              {[
                <><b>SurgGraph (Autonomous Workflow)</b> watches the surgery, autonomously detects errors, reasons complications, and writes real-time FHIR records</>,
                <><b>SurgBot (Feedback Workflow)</b> reviews every case with a surgeon and captures structured feedback</>,
                <><b>The feedback layer</b> turns that feedback into knowledge base that improves SurgGraph Agents</>,
              ].map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ul>
          </div>

          <div className="home__hero-visual-caption">
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              Gemini 3.5
            </span>
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              Vertex AI
            </span>
            <span className="home__hero-pill">
              <span className="home__hero-pill-dot" />
              Gemini Enterprise Agent Platform
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
