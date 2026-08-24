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
        <span className="home__nav-mark" />
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
