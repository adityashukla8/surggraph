import { useEffect, useState } from "react";
import {
  BOXES,
  CANVAS,
  EDGES,
  KIND_COLOR,
  KIND_LABEL,
  LEGEND_KINDS,
  NODES,
  type ArchEdge,
  type ArchNode,
  type Side,
} from "./architectureData";

// Renders architectureData.ts as one inline <svg>. Inline rather than a
// diagramming library because this is a static reference diagram, and a plain
// SVG can be serialised straight to a file (scripts/export_architecture.py) —
// so the page and docs/architecture/surgos-architecture.svg come from one
// render and cannot drift.
//
// Logos are optional: a node whose logo file is absent falls back to a
// monogram tile, so the diagram is complete and correct before any icon is
// added, and fills out as they arrive.

const byId = new Map(NODES.map((n) => [n.id, n]));

/** Anchor point on a node's edge. */
function port(n: ArchNode, s: Side) {
  switch (s) {
    case "t":
      return { x: n.x + n.w / 2, y: n.y };
    case "b":
      return { x: n.x + n.w / 2, y: n.y + n.h };
    case "l":
      return { x: n.x, y: n.y + n.h / 2 };
    case "r":
      return { x: n.x + n.w, y: n.y + n.h / 2 };
  }
}

/** Orthogonal routing. Architecture diagrams use right angles rather than
    curves: a right angle reads as "this connects to that", where a bezier
    reads as a vague association. */
function routeOf(e: ArchEdge): { d: string; mid: { x: number; y: number } } | null {
  const a = byId.get(e.from);
  const b = byId.get(e.to);
  if (!a || !b) return null;
  const p1 = port(a, e.fromSide);
  const p2 = port(b, e.toSide);
  const horiz = e.fromSide === "l" || e.fromSide === "r";
  let pts: { x: number; y: number }[];

  if (e.bend?.x !== undefined) {
    // Out to a chosen vertical corridor, then across — lets two edges leaving
    // the same port separate instead of drawing on top of each other.
    pts = [p1, { x: e.bend.x, y: p1.y }, { x: e.bend.x, y: p2.y }, p2];
  } else if (e.bend?.y !== undefined) {
    // Out, along a chosen corridor, then in — routes around the subsystem
    // boxes instead of straight through them. Works from any side.
    pts = [p1, { x: p1.x, y: e.bend.y }, { x: p2.x, y: e.bend.y }, p2];
  } else if (Math.abs(p1.x - p2.x) < 2 || Math.abs(p1.y - p2.y) < 2) {
    pts = [p1, p2];
  } else if (horiz) {
    // Turn shortly after leaving the source rather than half way: the label
    // then sits in the gap between boxes instead of over the target's header.
    const mx = p1.x + (p2.x > p1.x ? 34 : -34);
    pts = [p1, { x: mx, y: p1.y }, { x: mx, y: p2.y }, p2];
  } else {
    const my = p1.y + (p2.y > p1.y ? 26 : -26);
    pts = [p1, { x: p1.x, y: my }, { x: p2.x, y: my }, p2];
  }

  const d = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ");
  const i2 = Math.floor(pts.length / 2);
  const m = pts[i2];
  const m2 = pts[Math.max(0, i2 - 1)];
  return { d, mid: { x: (m.x + m2.x) / 2, y: (m.y + m2.y) / 2 } };
}

function wrap(text: string, maxChars: number): string[] {
  const words = text.split(" ");
  const lines: string[] = [];
  let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > maxChars && cur) {
      lines.push(cur);
      cur = w;
    } else {
      cur = (cur + " " + w).trim();
    }
  }
  if (cur) lines.push(cur);
  return lines;
}

function NodeBox({ n, logos }: { n: ArchNode; logos: Set<string> }) {
  const c = KIND_COLOR[n.kind];
  const hasLogo = !!n.logo && logos.has(n.logo);
  const iconX = n.x + 14;
  const iconY = n.y + 15;
  const textX = iconX + 42;
  const labelLines = wrap(n.label, Math.floor((n.w - 66) / 7.8));
  const roleLines = n.role ? wrap(n.role, Math.floor((n.w - 66) / 6.1)) : [];
  return (
    <g>
      <rect x={n.x} y={n.y} width={n.w} height={n.h} rx={10} fill="#fff" stroke={c} strokeWidth={1.5} />
      {hasLogo ? (
        <image href={`/tech-logos/${n.logo}`} x={iconX} y={iconY} width={30} height={30} preserveAspectRatio="xMidYMid meet" />
      ) : (
        <>
          <rect x={iconX} y={iconY} width={30} height={30} rx={7} fill={c} opacity={0.13} />
          <text x={iconX + 15} y={iconY + 20} textAnchor="middle" fontSize={11} fontWeight={700} fill={c}>
            {n.mono ?? n.label.slice(0, 2)}
          </text>
        </>
      )}
      {labelLines.map((l, i) => (
        <text key={`t${i}`} x={textX} y={n.y + 27 + i * 15} fontSize={14.5} fontWeight={700} fill="#0b1220">
          {l}
        </text>
      ))}
      {roleLines.map((l, i) => (
        <text key={i} x={textX} y={n.y + 43 + (labelLines.length - 1) * 15 + i * 13} fontSize={11.5} fill="#4b5563">
          {l}
        </text>
      ))}
      {n.detail?.map((d, i) => (
        <text key={`d${i}`} x={textX} y={n.y + 48 + (labelLines.length - 1) * 15 + roleLines.length * 13 + i * 13} fontSize={10.5} fill="#8b93a1">
          {d}
        </text>
      ))}
    </g>
  );
}

export function ArchitectureDiagram() {
  const [logos, setLogos] = useState<Set<string>>(new Set());
  useEffect(() => {
    const names = [...new Set(NODES.map((n) => n.logo).filter(Boolean) as string[])];
    let alive = true;
    Promise.all(
      names.map(
        (f) =>
          new Promise<string | null>((res) => {
            const img = new Image();
            img.onload = () => res(f);
            img.onerror = () => res(null);
            img.src = `/tech-logos/${f}`;
          }),
      ),
    ).then((found) => alive && setLogos(new Set(found.filter(Boolean) as string[])));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <svg
      id="surgos-architecture"
      viewBox={`0 0 ${CANVAS.w} ${CANVAS.h}`}
      width="100%"
      xmlns="http://www.w3.org/2000/svg"
      style={{ background: "#fff", display: "block" }}
      fontFamily="Poppins, sans-serif"
    >
      <defs>
        <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#7c8798" />
        </marker>
        <marker id="arw-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={KIND_COLOR.data} />
        </marker>
      </defs>

      <text x={40} y={40} fontSize={25} fontWeight={800} fill="#0b1220">
        SurgOS — System Architecture
      </text>
      <text x={40} y={62} fontSize={12.5} fill="#4b5563">
        Numbers follow one case end to end.
      </text>

      {/* Legend */}
      {LEGEND_KINDS.map((k, i) => {
        const lx = 980 + (i % 3) * 310;
        const ly = 24 + Math.floor(i / 3) * 17;
        return (
          <g key={k}>
            <rect x={lx} y={ly - 8} width={10} height={10} rx={2.5} fill="#fff" stroke={KIND_COLOR[k]} strokeWidth={2} />
            <text x={lx + 15} y={ly + 1} fontSize={11} fill="#4b5563">
              {KIND_LABEL[k]}
            </text>
          </g>
        );
      })}

      {/* Containers first, so nesting reads correctly */}
      {BOXES.map((b) => (
        <g key={b.id}>
          <rect
            x={b.x}
            y={b.y}
            width={b.w}
            height={b.h}
            rx={16}
            fill={b.tint}
            stroke={b.border}
            strokeWidth={b.boundary ? 1.8 : 1.2}
            strokeDasharray={b.boundary ? "8 5" : undefined}
          />
          <text
            x={b.x + 18}
            y={b.y + (b.boundary ? 26 : 24)}
            fontSize={b.boundary ? 14.5 : 13.5}
            fontWeight={700}
            fill={b.boundary ? "#334155" : "#0b1220"}
          >
            {b.label}
          </text>
          {b.sub && (
            <text x={b.x + 18} y={b.y + 40} fontSize={11} fill="#64748b">
              {b.sub}
            </text>
          )}
        </g>
      ))}

      {/* Edges beneath the nodes, so a line never crosses a node label */}
      {EDGES.map((e, i) => {
        const r = routeOf(e);
        if (!r) return null;
        const stroke = e.accent ? KIND_COLOR.data : "#7c8798";
        const halfW = e.label ? e.label.length * 3.15 + (e.step !== undefined ? 15 : 7) : 0;
        return (
          <g key={i}>
            <path
              d={r.d}
              fill="none"
              stroke={stroke}
              strokeWidth={e.accent ? 2 : 1.5}
              strokeDasharray={e.async ? "6 4" : e.accent ? "7 4" : undefined}
              markerEnd={e.accent ? "url(#arw-a)" : "url(#arw)"}
            />
            {e.label && (
              <g>
                <rect
                  x={r.mid.x - halfW}
                  y={r.mid.y - 10.5}
                  width={halfW * 2}
                  height={21}
                  rx={10.5}
                  fill="#fff"
                  stroke={stroke}
                  strokeWidth={0.8}
                  opacity={0.97}
                />
                {e.step !== undefined && (
                  <>
                    <circle cx={r.mid.x - halfW + 10} cy={r.mid.y} r={8.5} fill={stroke} />
                    <text x={r.mid.x - halfW + 10} y={r.mid.y + 3} textAnchor="middle" fontSize={10} fontWeight={800} fill="#fff">
                      {e.step}
                    </text>
                  </>
                )}
                <text
                  x={r.mid.x + (e.step !== undefined ? 8 : 0)}
                  y={r.mid.y + 3.5}
                  textAnchor="middle"
                  fontSize={11}
                  fontWeight={600}
                  fill="#334155"
                >
                  {e.label}
                </text>
              </g>
            )}
          </g>
        );
      })}

      {NODES.map((n) => (
        <NodeBox key={n.id} n={n} logos={logos} />
      ))}
    </svg>
  );
}
