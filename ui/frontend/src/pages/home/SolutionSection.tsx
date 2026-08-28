interface SolutionCard {
  key: string;
  num: string;
  name: string;
  title: string;
  subtitle?: string;
  bullets: string[];
  icon: React.ReactNode;
}

const CARDS: SolutionCard[] = [
  {
    key: "surggraph",
    num: "01",
    name: "SurgGraph",
    title: "The Autonomous Workflow",
    subtitle: "Core TaskMaster submission",
    bullets: [
      "9 specialist agents reason live over surgical video",
      "Detects errors → reasons complications → drafts corrective plans",
      "Writes FHIR records + alerts, fail-closed by design",
    ],
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
        <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="1.7" />
        <path
          d="M12 3v2.4M12 18.6V21M21 12h-2.4M5.4 12H3M18.1 5.9l-1.7 1.7M7.6 16.4l-1.7 1.7M18.1 18.1l-1.7-1.7M7.6 7.6 5.9 5.9"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      </svg>
    ),
  },
  {
    key: "surgbot",
    num: "02",
    name: "SurgBot",
    title: "The Feedback Workflow",
    bullets: [
      "Conversational, voice-driven review of a completed case with a surgeon",
      "Captures structured agree / disagree feedback per finding",
      "Drafts an approvable case-review document",
    ],
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
        <path
          d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7A2.5 2.5 0 0 1 17.5 15H10l-4.5 4v-4H6.5A2.5 2.5 0 0 1 4 12.5z"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
  {
    key: "loop",
    num: "03",
    name: "The Feedback Layer",
    title: "Closes the Circuit",
    bullets: [
      "Approved feedback is routed and stored as retrievable knowledge",
      "SurgGraph's agents consult it on every future case",
      "State-driven — routed from real case graph data",
      "Advisory only — never auto-suppresses a live alert",
    ],
    icon: (
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
        <path
          d="M4 12a8 8 0 0 1 13.66-5.66M20 12a8 8 0 0 1-13.66 5.66"
          stroke="currentColor"
          strokeWidth="1.7"
          strokeLinecap="round"
        />
        <path d="M17.5 3v3.5H14M6.5 21v-3.5H10" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    ),
  },
];

function Card({ card, wide, gridArea }: { card: SolutionCard; wide?: boolean; gridArea?: string }) {
  return (
    <div
      className={`home__solution-card${wide ? " home__solution-card--wide" : ""}`}
      style={gridArea ? { gridArea } : undefined}
    >
      <div className="home__solution-card-top">
        <span className="home__solution-card-icon">{card.icon}</span>
        <span className="home__solution-card-num">{card.num} · {card.name}</span>
      </div>
      <h3 className="home__solution-card-title">{card.title}</h3>
      {card.subtitle && <span className="home__solution-card-subtitle">{card.subtitle}</span>}
      <ul className="home__solution-card-bullets">
        {card.bullets.map((b) => (
          <li key={b}>{b}</li>
        ))}
      </ul>
    </div>
  );
}

export function SolutionSection() {
  return (
    <section className="home__section" id="solution">
      <span className="home__eyebrow">The Solution</span>
      <h2 className="home__headline" style={{ fontSize: 34, maxWidth: 620 }}>
        1 System. 3 Components.
      </h2>

      <div className="home__solution-diagram">
        <div className="home__solution-loop-gutter" aria-hidden="true">
          <span className="home__solution-loop-arrowhead">▲</span>
          <span className="home__solution-loop-line" />
          <span className="home__solution-loop-label">feeds back</span>
        </div>

        <Card card={CARDS[0]} gridArea="card1" />
        <span className="home__solution-arrow home__solution-arrow--h" aria-hidden="true">→</span>
        <Card card={CARDS[1]} gridArea="card2" />

        <span className="home__solution-arrow home__solution-arrow--v" aria-hidden="true">↓</span>

        <Card card={CARDS[2]} wide />
      </div>
    </section>
  );
}
