import "../home/home.css";
import { ArchitectureDiagram } from "./ArchitectureDiagram";

// The earlier SVG topology view, kept on its own route.
//
// It is not the primary diagram — /architecture renders the DOM board, which
// cannot overlap and has readable browser type. This one is retained because
// it does one thing the board does not: it is a single self-contained vector
// file, so it scales losslessly for print and can be opened or edited outside
// this app. scripts/export_architecture.py --variant svg serialises it to
// docs/architecture/.

export function ArchitectureSvgPage() {
  return (
    <div className="home" style={{ padding: 24, background: "#fff" }}>
      <ArchitectureDiagram />
    </div>
  );
}
