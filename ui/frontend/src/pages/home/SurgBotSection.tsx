import { SurgBotFlow } from "./SurgBotFlow";

export function SurgBotSection() {
  return (
    <section className="home__section" id="surgbot">
      <span className="home__eyebrow">SurgBot</span>
      <h2 className="home__headline" style={{ fontSize: 38, maxWidth: 780 }}>
        Cross-case QA Assistant for Surgeons &amp; Safety Teams
      </h2>
      <p className="home__lede" style={{ marginBottom: 16, maxWidth: 760 }}>
        SurgBot is a conversational layer that lets surgeons and QA teams talk through what SurgGraph already
        captured:
      </p>
      <ul className="home__lede-list" style={{ marginBottom: 40, maxWidth: 760 }}>
        <li>revisit past surgeries</li>
        <li>surface patterns across cases</li>
        <li>dig into flagged errors and complications</li>
        <li>provide per-case feedback</li>
      </ul>
      <div className="home__surgbot-flow-wrap">
        <SurgBotFlow />
      </div>
    </section>
  );
}
