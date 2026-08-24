const CENTER = { x: 120, y: 120 };

// Two rings of nodes slowly orbiting a center hub — same visual idea as
// opensaas.sh's hero (concentric rings, small nodes drifting around them at
// different speeds/directions), standing in here for the orchestrator
// continuously coordinating its specialist agents. Purely decorative;
// deliberately oversized and cropped by the card's edge (see
// .home__orchestrator-flow-wrap) rather than boxed neatly inside it.

const RING1_R = 50;
const RING1_DOTS = [
  { x: 158.3, y: 152.15 },
  { x: 87.85, y: 158.3 },
  { x: 81.7, y: 87.85 },
  { x: 152.15, y: 81.7 },
];

const RING2_R = 92;
const RING2_DOTS = [
  { x: 212.0, y: 120.0 },
  { x: 148.4, y: 207.5 },
  { x: 45.6, y: 174.1 },
  { x: 45.6, y: 65.9 },
  { x: 148.4, y: 32.5 },
];

export function OrchestratorFlow() {
  return (
    <svg className="home__orchestrator-flow" viewBox="0 0 240 240" aria-hidden="true">
      <circle cx={CENTER.x} cy={CENTER.y} r={RING1_R} className="home__orbit-ring" />
      <circle cx={CENTER.x} cy={CENTER.y} r={RING2_R} className="home__orbit-ring" />

      <g>
        {RING1_DOTS.map((d, i) => (
          <circle key={i} cx={d.x} cy={d.y} r="5" className="home__orbit-dot home__orbit-dot--ring1" />
        ))}
        <animateTransform
          attributeName="transform"
          type="rotate"
          from={`0 ${CENTER.x} ${CENTER.y}`}
          to={`360 ${CENTER.x} ${CENTER.y}`}
          dur="22s"
          repeatCount="indefinite"
        />
      </g>

      <g>
        {RING2_DOTS.map((d, i) => (
          <circle key={i} cx={d.x} cy={d.y} r="4.5" className="home__orbit-dot home__orbit-dot--ring2" />
        ))}
        <animateTransform
          attributeName="transform"
          type="rotate"
          from={`360 ${CENTER.x} ${CENTER.y}`}
          to={`0 ${CENTER.x} ${CENTER.y}`}
          dur="32s"
          repeatCount="indefinite"
        />
      </g>

      <circle cx={CENTER.x} cy={CENTER.y} r="10" className="home__orchestrator-hub" />
    </svg>
  );
}
