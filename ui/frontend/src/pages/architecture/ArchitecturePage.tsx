import "../home/home.css";
import { Nav } from "../home/Nav";
import { Footer } from "../home/Footer";
import "./architecture.css";
import { ArchitectureBoard } from "./ArchitectureBoard";

// The on-site half of the architecture deliverable. The other half is
// docs/architecture/surgos-architecture.svg, serialised from this very page by
// scripts/export_architecture.py — so the file in the repo is never a
// hand-maintained copy that can fall out of date.

export function ArchitecturePage() {
  return (
    <div className="home">
      <Nav />
      <section className="home__section arch__section" id="architecture">
        <span className="home__eyebrow">Architecture</span>
        <h1 className="home__headline" style={{ fontSize: 38, margin: "0 0 12px" }}>
          SurgOS, end to end
        </h1>
        <p className="home__lede" style={{ marginBottom: 28 }}>
          Every service, model, agent, gate and external write this system uses. The numbered walkthrough below follows one case from video to filed record and back
          again as feedback.
        </p>
        <div className="arch__frame">
          <ArchitectureBoard />
        </div>
      </section>
      <Footer />
    </div>
  );
}
