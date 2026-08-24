import { Link } from "react-router-dom";

export function DemoCtaSection() {
  return (
    <section className="home__section">
      <div className="home__demo-cta">
        <h2 className="home__demo-cta-headline">See SurgGraph analyze a real robotic prostatectomy.</h2>
        <p className="home__demo-cta-body">
          Watch the autonomous workflow in real time. See errors detected, complications reasoned with citations,
          corrective proposals surface, and — at case close — the operative note draft ready for surgeon approval.
        </p>
        <Link to="/console" className="home__btn home__btn--primary home__demo-cta-btn">
          ▶ Launch Live Demo
        </Link>
      </div>
    </section>
  );
}
