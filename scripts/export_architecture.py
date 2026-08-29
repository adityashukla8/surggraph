"""Exports the architecture board from /architecture to docs/architecture/.

The diagram lives in exactly ONE place — ui/frontend/src/pages/architecture/
ArchitectureBoard.tsx — and this script screenshots that page, so the image
committed to the repo can never drift from the page on the site.

It is a DOM/CSS board rather than a hand-positioned SVG on purpose: in the SVG
version every box coordinate was a literal, so text that grew by one word
overlapped its neighbour and every connector needed a hand-tuned corridor.
CSS Grid cannot overlap, and the type is real browser type rather than scaled
canvas units.

Usage:
  uv run --with playwright python3 scripts/export_architecture.py [--url URL]
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

GOOGLE_FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=Poppins:ital,wght@"
    "0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,400&display=swap"
)

REPO = Path(__file__).resolve().parent.parent
BOARD = REPO / "ui" / "frontend" / "src" / "pages" / "architecture" / "ArchitectureBoard.tsx"
# The board reads from /icons; the older vector page still reads from
# /tech-logos. They are different directories and different diagrams.
ICON_DIR = REPO / "ui" / "frontend" / "public" / "icons"
# The home page shows the exported PNG rather than re-rendering the board, so
# the export has to land somewhere Vite serves. Written from the same run as
# the docs copy, so the two cannot drift.
SITE_DIR = REPO / "ui" / "frontend" / "public"
LOGO_DIR = REPO / "ui" / "frontend" / "public" / "tech-logos"
OUT_DIR = REPO / "docs" / "architecture"


def referenced_logos() -> list[str]:
    """Logo filenames the board asks for, read straight from its source."""
    return sorted(set(re.findall(r'logo:\s*"([^"]+)"', BOARD.read_text())))


def export_svg(args) -> int:
    """Serialises the vector variant to a self-contained .svg.

    Logos are inlined as data URIs: a bare /tech-logos/ path only resolves
    while the dev server is running, so the committed file would render with
    broken images anywhere else.
    """
    url = args.url.replace("/architecture", "/architecture/svg")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = OUT_DIR / "surgos-architecture-topology.svg"
    png_path = OUT_DIR / "surgos-architecture-topology.png"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1980, "height": 1400}, device_scale_factor=args.scale)
        page.goto(url, wait_until="networkidle")
        page.wait_for_selector("#surgos-architecture")
        page.wait_for_timeout(1500)
        svg = page.eval_on_selector("#surgos-architecture", "el => el.outerHTML")
        page.query_selector("#surgos-architecture").screenshot(path=str(png_path))
        browser.close()

    missing: list[str] = []

    def inline(m: re.Match) -> str:
        name = m.group(1)
        f = LOGO_DIR / name
        if not f.exists():
            missing.append(name)
            return m.group(0)
        mime = mimetypes.guess_type(str(f))[0] or "image/png"
        return f'href="data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}"'

    svg = re.sub(r'href="/tech-logos/([^"]+)"', inline, svg)
    if "xmlns=" not in svg.split(">")[0]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    svg_path.write_text(svg)

    print(f"{svg_path}  ({svg_path.stat().st_size // 1024} KB, vector)")
    print(f"{png_path}  ({png_path.stat().st_size // 1024} KB, {args.scale}x)")
    if missing:
        print(f"\n{len(set(missing))} logo(s) missing — rendered as monograms")
    return 0


# The page background is the site's blue-grey and the nav is sticky, so both
# would land in the capture; stripped so the asset drops cleanly into a slide
# or a doc. The numbered walkthrough is optional — it reads as part of the
# figure in a document, and as clutter on a slide.
CAPTURE_CSS = """
  /* The nav is sticky, so it paints over the top of the board and lands in an
     element screenshot even though it is not part of it. */
  .home__nav { display: none !important; }
  html, body, .home, .arch__section, .arch__frame { background: #fff !important; }
  .arch__frame { border: 0 !important; }
  #surgos-architecture-board { background: #fff !important; padding: 40px !important; }
"""
HIDE_WALKTHROUGH_CSS = ".ab__flow { display: none !important; }"

# A title block for the exported asset only — the live page carries the same
# information in its nav and footer, and would read as a watermark with this
# on it too. Prepended as a normal block rather than an overlay so it can
# never land on top of the diagram if the layout moves.
CREDIT_CSS = """
  .ab__credit {
    display: flex;
    flex-direction: column;
    gap: 3px;
    margin: 0 0 26px;
    font-family: "Poppins", system-ui, sans-serif;
    color: #0b1220;
  }
  .ab__credit b { font-size: 19px; font-weight: 700; letter-spacing: -0.01em; }
  .ab__credit span { font-size: 13px; font-weight: 500; color: #4b5563; }
"""

CREDIT_JS = """() => {
     const board = document.querySelector('#surgos-architecture-board');
     if (board.querySelector('.ab__credit')) return;
     const el = document.createElement('div');
     el.className = 'ab__credit';
     const title = document.createElement('b');
     title.textContent = 'SurgOS: All Things Agentic Hackathon';
     const by = document.createElement('span');
     by.textContent = 'Submitted by: Aditya Shukla (linkedin.com/in/adityashukla8)';
     el.append(title, by);
     board.prepend(el);
   }"""


def capture_css(with_walkthrough: bool) -> str:
    css = CAPTURE_CSS + CREDIT_CSS
    return css if with_walkthrough else css + HIDE_WALKTHROUGH_CSS


def latin_font_face_css(page) -> str:
    """Poppins, inlined as data URIs.

    The Google Fonts sheet is cross-origin, so its rules are unreadable from
    the page and a bare @import would leave the SVG depending on the network.
    Only the latin blocks are kept — the sheet also carries devanagari and
    latin-ext, which this diagram never uses.
    """
    css = page.context.request.get(GOOGLE_FONTS_URL).text()
    faces, kept = [], 0
    for block in re.findall(r"@font-face\s*\{[^}]*\}", css):
        if "unicode-range" in block and "U+0000-00FF" not in block:
            continue
        m = re.search(r"url\((https://[^)]+\.woff2)\)", block)
        if not m:
            continue
        data = page.context.request.get(m.group(1)).body()
        faces.append(block.replace(m.group(1), f"data:font/woff2;base64,{base64.b64encode(data).decode()}"))
        kept += 1
    return "\n".join(faces), kept


def export_diagram_svg(page, css_w: float, css_h: float, css_extra: str, stem: str) -> Path:
    """The same board, wrapped in <foreignObject> so it stays resolution-free.

    This is real HTML inside an SVG, not vector shapes: it renders in browsers
    and anything Chromium-based, but editors that ignore foreignObject
    (Illustrator, Inkscape) will show an empty frame. The PNG beside it is the
    portable one.
    """
    fonts, n_faces = latin_font_face_css(page)
    css = page.evaluate(
        r"""() => {
             const out = [];
             for (const sheet of document.styleSheets) {
               if (sheet.href) continue;       // cross-origin: rules unreadable
               try { for (const rule of sheet.cssRules) out.push(rule.cssText); } catch (e) {}
             }
             return out.join("\n");
           }"""
    )
    # XMLSerializer, not outerHTML: an .svg is parsed as XML, where HTML's
    # unclosed void tags (<img>, <br>) are a fatal error.
    html = page.evaluate(
        """() => {
             const el = document.querySelector('#surgos-architecture-board').cloneNode(true);
             // Logos are <img src="/icons/..."> — only resolvable while the dev
             // server is up, so they are swapped for data URIs below.
             return new XMLSerializer().serializeToString(el);
           }"""
    )

    def inline(m: re.Match) -> str:
        f = ICON_DIR / m.group(1)
        if not f.exists():
            return m.group(0)
        mime = mimetypes.guess_type(str(f))[0] or "image/png"
        return f'src="data:{mime};base64,{base64.b64encode(f.read_bytes()).decode()}"'

    html, n_logos = re.subn(r'src="/icons/([^"]+)"', inline, html)
    body = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{css_w:.0f}" height="{css_h:.0f}" '
        f'viewBox="0 0 {css_w:.0f} {css_h:.0f}">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<foreignObject x="0" y="0" width="100%" height="100%">'
        f'<div xmlns="http://www.w3.org/1999/xhtml">'
        # CDATA: the app's CSS carries @property descriptors like syntax:
        # "<angle>", which an XML parser would otherwise read as a tag.
        f"<style><![CDATA[{fonts}\n{css}\n{css_extra}]]></style>{html}</div>"
        f"</foreignObject></svg>"
    )
    out = OUT_DIR / f"{stem}.svg"
    out.write_text(body)
    print(f"{out}  ({out.stat().st_size // 1024} KB, {css_w:.0f} x {css_h:.0f}, "
          f"{n_logos} logos + {n_faces} font faces inlined)")
    return out


def export_diagram(args) -> int:
    """The diagram on its own, on white, scaled to land on exactly --target-width.

    Two passes: the first measures the board at scale 1, the second re-renders
    at the device pixel ratio that turns that CSS width into the requested
    pixel width — so the output is a real 4K raster, not an upscale.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = "surgos-architecture-full" if args.walkthrough else "surgos-architecture-diagram"
    css_extra = capture_css(args.walkthrough)
    png = OUT_DIR / f"{stem}-4k.png"

    def prepare(page):
        page.goto(args.url, wait_until="networkidle")
        page.add_style_tag(content=css_extra)
        page.wait_for_selector("#surgos-architecture-board")
        page.evaluate(CREDIT_JS)
        # The board probes which logo files exist and re-measures its own
        # arrows through a ResizeObserver; let both settle.
        page.wait_for_timeout(2000)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 1400})
        prepare(page)
        css_w = page.eval_on_selector("#surgos-architecture-board", "el => el.getBoundingClientRect().width")
        page.close()

        scale = args.target_width / css_w
        page = browser.new_page(viewport={"width": args.width, "height": 1400}, device_scale_factor=scale)
        prepare(page)
        el = page.query_selector("#surgos-architecture-board")
        box = el.bounding_box()
        el.screenshot(path=str(png))

        # The SVG is built from a scale-1 render: foreignObject carries CSS
        # pixels, so a device-pixel-ratio pass would bake the ratio into it.
        svg_page = browser.new_page(viewport={"width": args.width, "height": 1400})
        prepare(svg_page)
        sbox = svg_page.query_selector("#surgos-architecture-board").bounding_box()
        export_diagram_svg(svg_page, sbox["width"], sbox["height"], css_extra, stem)
        browser.close()

    served = SITE_DIR / png.name
    served.write_bytes(png.read_bytes())

    print(f"{png}  ({png.stat().st_size // 1024} KB)")
    print(f"{served}  (served to the site at /{png.name})")
    print(f"  {round(box['width'] * scale)} x {round(box['height'] * scale)} px  "
          f"({box['width']:.0f} CSS px at {scale:.2f}x, white background, "
          f"{'with' if args.walkthrough else 'no'} walkthrough)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5173/architecture")
    ap.add_argument("--scale", type=int, default=2, help="PNG pixel density")
    ap.add_argument("--width", type=int, default=1780)
    ap.add_argument(
        "--variant",
        choices=["board", "svg", "diagram"],
        default="diagram",
        help="board = the DOM diagram at /architecture (primary); "
        "svg = the vector topology view at /architecture/svg, written as a "
        "self-contained .svg for print and offline editing; "
        "diagram = the diagram alone on white at --target-width, no walkthrough",
    )
    ap.add_argument("--target-width", type=int, default=3840, help="diagram variant: output pixel width")
    ap.add_argument(
        "--walkthrough",
        action="store_true",
        help="diagram variant: keep the numbered walkthrough under the diagram "
        "(writes surgos-architecture-full.* instead of surgos-architecture-diagram.*)",
    )
    args = ap.parse_args()

    if args.variant == "svg":
        return export_svg(args)
    if args.variant == "diagram":
        return export_diagram(args)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png_path = OUT_DIR / "surgos-architecture.png"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": 1400}, device_scale_factor=args.scale)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("#surgos-architecture-board")
        # The board probes which logo files exist before rendering them; let
        # those image loads settle so the export matches what a visitor sees.
        page.wait_for_timeout(1500)
        page.query_selector("#surgos-architecture-board").screenshot(path=str(png_path))
        browser.close()

    print(f"{png_path}  ({png_path.stat().st_size // 1024} KB, {args.scale}x, {args.width}px wide)")

    missing = [f for f in referenced_logos() if not (ICON_DIR / f).exists()]
    if missing:
        print(f"\n{len(missing)} logo(s) referenced but not present — rendered as monograms:")
        for m in missing:
            print(f"  - {m}")
        print(f"drop them in {ICON_DIR.relative_to(REPO)}/ and re-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
