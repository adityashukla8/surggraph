interface ProblemCard {
  color: string;
  icon: string;
  label: string;
  title: string;
  body: React.ReactNode;
  pairing: string;
  citationLabel: string;
  citationHref: string;
}

const EXAMPLE_STEPS = [
  {
    label: "Surgical Error",
    color: "var(--home-red)",
    text: "Needle lost during vesicourethral anastomosis, pulled through a 12mm trocar without visualization",
  },
  {
    label: "Complication",
    color: "var(--home-red)",
    text: "Retained surgical needle in the abdominal cavity — risk of chronic pain, bowel or vascular injury",
  },
  {
    label: "Patient Outcome",
    color: "var(--home-yellow)",
    text: "~10-minute laparoscopic search located the needle on the abdominal wall; retrieved, no post-op complications",
  },
];

const CARDS: ProblemCard[] = [
  {
    color: "var(--home-accent)",
    icon: "⚠",
    label: "Technique Errors",
    title: "One missed step, a cascading complication",
    body: (
      <>
        In a nationwide study of abdominal surgery, patients who had an intraoperative adverse event went on to
        develop a Grade II-or-higher postoperative complication <b>26.4% of the time</b> — versus <b>12%</b> for
        patients whose case had none. The error doesn't stay contained to the moment it happens.
      </>
    ),
    pairing: "SurgGraph's Error Detection agent catches the technique error live, in the window it happens.",
    citationLabel: "Gawria et al., 2023 · PMC10095268",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC10095268/",
  },
  {
    color: "var(--home-accent)",
    icon: "◈",
    label: "Complication Risk",
    title: "Complications are common, and hard to see coming",
    body: (
      <>
        Across 7 studies and 4,122 radical prostatectomy patients — robot-assisted procedures included — larger,
        more technically demanding anatomy saw complications in <b>17%</b> of cases, versus <b>10%</b> for
        lower-risk anatomy. The risk is real and it's specific to the patient in front of you, not a generic average.
      </>
    ),
    pairing: "SurgGraph reasons about complication risk from the patient's own anatomy, live, against literature.",
    citationLabel: "Fahmy et al., 2021 · PMC8656835",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC8656835/",
  },
  {
    color: "var(--home-accent)",
    icon: "▤",
    label: "Documentation Lag",
    title: "The record arrives long after the case is over",
    body: (
      <>
        Dictated operative reports took a mean of <b>374 hours — over 15 days</b> — to reach a verified, signed
        state, against <b>28 minutes</b> for structured, template-driven notes. The clinical record that other
        clinicians rely on is routinely two weeks out of date.
      </>
    ),
    pairing: "SurgGraph drafts the operative note as a byproduct of reasoning that already happened, at case close.",
    citationLabel: "Laflamme et al., 2005 · PMC1560865",
    citationHref: "https://pmc.ncbi.nlm.nih.gov/articles/PMC1560865/",
  },
];

export function ProblemSection() {
  return (
    <section className="home__section" id="problem">
      <div className="home__problem-intro">
        <div>
          <span className="home__eyebrow">The Problem</span>
          <h2 className="home__headline" style={{ fontSize: 34, maxWidth: 620 }}>
            Surgical errors compound into complications.
            <br />
            <br></br>
            Complications impact patient outcomes.
            <br />
            <br></br>
            Documentation trails days behind the case that needed it.
          </h2>
        </div>

        <div className="home__example-flow">
          <span className="home__example-flow-eyebrow">A real, published case</span>
          {EXAMPLE_STEPS.map((s, i, arr) => (
            <div className="home__example-step-wrap" key={s.label}>
              <div className="home__example-step" style={{ ["--step-color" as string]: s.color }}>
                <span className="home__example-step-label">{s.label}</span>
                <p className="home__example-step-text">{s.text}</p>
              </div>
              {i < arr.length - 1 && <span className="home__example-arrow">↓</span>}
            </div>
          ))}
          <a
            href="https://pmc.ncbi.nlm.nih.gov/articles/PMC10436752/"
            target="_blank"
            rel="noreferrer"
            className="home__example-citation"
          >
            ↗ Koida et al., 2023 · Cureus · PMC10436752
          </a>
        </div>
      </div>

      <div className="home__problem-grid">
        {CARDS.map((c) => (
          <div className="home__card" key={c.title}>
            <div className="home__card-icon" style={{ ["--card-color" as string]: c.color }}>
              {c.icon}
            </div>
            <div className="home__card-label">{c.label}</div>
            <h3 className="home__card-title">{c.title}</h3>
            <div className="home__card-body">{c.body}</div>
            <hr className="home__card-divider" />
            <div className="home__card-pairing">→ {c.pairing}</div>
            <a href={c.citationHref} target="_blank" rel="noreferrer" className="home__card-citation">
              ↗ {c.citationLabel}
            </a>
          </div>
        ))}
      </div>
    </section>
  );
}
