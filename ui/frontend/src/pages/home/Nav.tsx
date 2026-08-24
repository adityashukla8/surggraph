import { Link } from "react-router-dom";

const LINKS = [
  { href: "#problem", label: "Problem" },
  { href: "#how-it-works", label: "How It Works" },
  { href: "#agents", label: "Agents" },
  { href: "#safety", label: "Safety" },
  { href: "#tech", label: "Tech" },
];

export function Nav() {
  return (
    <nav className="home__nav">
      <a href="#top" className="home__nav-brand">
        <span className="home__nav-mark">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none">
            <g stroke="#fff" strokeWidth="1.6" strokeLinecap="round">
              <line x1="12" y1="5.5" x2="6" y2="16.5" />
              <line x1="12" y1="5.5" x2="18" y2="16.5" />
              <line x1="6" y1="16.5" x2="18" y2="16.5" />
            </g>
            <circle cx="12" cy="5.5" r="2.4" fill="#fff" />
            <circle cx="6" cy="16.5" r="2.4" fill="#fff" />
            <circle cx="18" cy="16.5" r="2.4" fill="#fff" />
          </svg>
        </span>
        SURGGRAPH
      </a>
      <div className="home__nav-links">
        {LINKS.map((l) => (
          <a key={l.href} href={l.href} className="home__nav-link">
            {l.label}
          </a>
        ))}
        <Link to="/console" className="home__btn home__btn--primary">
          Launch Case
        </Link>
      </div>
    </nav>
  );
}
