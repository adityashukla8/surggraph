import "../architecture/architecture.css";
import { ArchitectureWalkthrough } from "../architecture/ArchitectureBoard";

// A still of the board rather than the board itself: the live version measures
// its own arrows against the DOM on every resize, and at this section's width
// it reflows into a different (worse) routing than the one it was tuned at.
// The PNG is written by scripts/export_architecture.py in the same run as the
// docs copy — re-run it after changing the board.
const SHOT = "/surgos-architecture-diagram-4k.png";

export function ArchitectureSection() {
  return (
    <section className="home__section" id="architecture">
      <span className="home__eyebrow">Architecture</span>
      <h2 className="home__headline" style={{ fontSize: 38, margin: "0 0 20px" }}>
        SurgOS, end to end
      </h2>
      <p className="home__lede" style={{ marginBottom: 28 }}>
        Every service, model, agent, gate and external write this system actually uses. The numbered walkthrough below follows one case from video to filed record and back again as
        feedback. Click the image for the full-size, interactive version.
      </p>
      <a
        className="arch__shot"
        href="/architecture"
        target="_blank"
        rel="noreferrer"
        aria-label="Open the full architecture diagram in a new tab"
      >
        <img src={SHOT} alt="SurgOS architecture — every service, model, agent and external write, with the 18 numbered steps of one case" />
        <span className="arch__shot-hint">Open full diagram &#8599;</span>
      </a>
      <ArchitectureWalkthrough />
    </section>
  );
}
