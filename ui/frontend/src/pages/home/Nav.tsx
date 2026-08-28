import { Link } from "react-router-dom";

const LINKS = [
  { href: "#problem", label: "Problem" },
  { href: "#solution", label: "Solution" },
  { href: "#how-it-works", label: "SurgGraph" },
  { href: "#agents", label: "Orchestration" },
  { href: "#state", label: "State" },
  { href: "#surgbot", label: "SurgBot" },
  { href: "#tech", label: "Tech" },
];

export function Nav() {
  return (
    <nav className="home__nav">
      <a href="#top" className="home__nav-brand">
        <span className="home__nav-mark">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none">
            <circle cx="12" cy="12" r="6" fill="#fff" />
          </svg>
        </span>
        SurgOS
      </a>
      <div className="home__nav-links">
        {LINKS.map((l) => (
          <a key={l.href} href={l.href} className="home__nav-link">
            {l.label}
          </a>
        ))}
      </div>
      <Link to="/console" className="home__nav-cta home__btn home__btn--primary">
        Launch Case
      </Link>
    </nav>
  );
}
