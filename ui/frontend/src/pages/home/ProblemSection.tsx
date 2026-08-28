const STATEMENTS = [
  "Surgical errors compound into complications.",
  "Complications impact patient outcomes.",
  "Documentation trails days behind the case that needed it.",
  "Feedback from one case rarely reaches the next.",
];

interface ProblemCard {
  icon: string;
  label: string;
  title: string;
  body: React.ReactNode;
  benefitHeader: string;
  pairing: string;
  citationLabel: string;
  citationHref: string;
}

const CARDS: ProblemCard[] = [
  {
    icon: "/icons/error.png",
    label: "Technique Errors",
    title: "One missed step, a cascading complication",
    body: (
      <>
        <b>26.4%</b> of patients with an intraoperative adverse event go on to develop a Grade II-or-higher
        complication — versus <b>12%</b> with none. One missed step rarely stays contained to the moment it happens.
      </>
    ),
    benefitHeader: "Turns hours-later review into real-time detection",
    pairing: "SurgGraph flags the technique error live, in the window it happens — not hours later in a post-op chart review.",
    citationLabel: "Gawria et al., 2023 · PMC10095268",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC10095268/",
  },
  {
    icon: "/icons/complication.png",
    label: "Complication Risk",
    title: "Complications are common, and hard to see coming",
    body: (
      <>
        <b>15%</b> of general surgery patients develop at least one complication — <b>6%</b> develop more than one.
        Complications aren't rare edge cases; they're a routine part of the risk every case carries.
      </>
    ),
    benefitHeader: "Turns a one-time checklist into continuous reasoning",
    pairing: "SurgGraph reasons about complication risk continuously — against the patient's own anatomy and live literature, not a one-time pre-op checklist.",
    citationLabel: "Tevis et al., 2016 · Annals of Surgery · PMC6214627",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC6214627/",
  },
  {
    icon: "/icons/documentation.png",
    label: "Documentation Lag",
    title: "The record arrives long after the case is over",
    body: (
      <>
        <b>374 hours — over 15 days</b> is the average time a dictated operative report takes to reach a verified,
        signed state. The record every other
        clinician relies on is routinely two weeks stale.
      </>
    ),
    benefitHeader: "Turns a 15-day wait into minutes",
    pairing: "SurgGraph drafts the note automatically at case close — turning a 15-day wait into minutes.",
    citationLabel: "Laflamme et al., 2005 · PMC1560865",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC1560865/",
  },
  {
    icon: "/icons/feedback-loop.svg",
    label: "Feedback Loop Failure",
    title: "Feedback rarely changes the next case",
    body: (
      <>
        <b>28%</b> of clinician feedback interventions improve care quality by 10% or more — the median gain across
        98 real trials is just <b>4.4 percentage points</b>. Lessons from one case routinely never reach the next.
      </>
    ),
    benefitHeader: "Turns one case's feedback into knowledge for every case",
    pairing: "SurgOS's Learning Loop routes every surgeon's feedback into knowledge which SurgGraph's agents automatically consult.",
    citationLabel: "Ivers et al., 2014 · J Gen Intern Med · PMC4238192",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC4238192/",
  },
];

export function ProblemSection() {
  return (
    <section className="home__section" id="problem">
      <span className="home__eyebrow">The Problem</span>
      <div className="home__problem-cascade">
        {STATEMENTS.map((s, i) => (
          <div className="home__problem-cascade-step" key={s}>
            <div className="home__problem-statement-card">
              <span className="home__problem-statement-num">0{i + 1}</span>
              <p className="home__problem-statement-text">{s}</p>
            </div>
            {i < STATEMENTS.length - 1 && (
              <span className="home__problem-cascade-arrow" aria-hidden="true">
                →
              </span>
            )}
          </div>
        ))}
      </div>

      <span className="home__eyebrow" style={{ marginTop: 56, display: "block" }}>
        Challenges SurgOS addresses
      </span>
      <div className="home__problem-grid">
        {CARDS.map((c) => (
          <div className="home__card" key={c.title}>
            <div className="home__card-top">
              <div className="home__card-icon">
                <img src={c.icon} alt="" />
              </div>
              <div className="home__card-label">{c.label}</div>
            </div>
            <h3 className="home__card-title">{c.title}</h3>
            <div className="home__card-body">{c.body}</div>
            <hr className="home__card-divider" />
            <div className="home__card-pairing">
              <span className="home__card-pairing-header">{c.benefitHeader}</span>
              → {c.pairing}
            </div>
            <a href={c.citationHref} target="_blank" rel="noreferrer" className="home__card-citation">
              ↗ {c.citationLabel}
            </a>
          </div>
        ))}
      </div>
    </section>
  );
}
