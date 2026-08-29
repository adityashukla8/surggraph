"""Screenshots the marketing home page, whole and per-section.

Exists so UI changes can be checked by looking at them rather than by
reasoning about CSS. Headed by default (WSLg is available, and watching the
run is often how a layout bug gets spotted); pass --headless for CI or when
no display is attached.

Usage:
  uv run --with playwright python3 scripts/snap_home.py [--headless]
                                                        [--url URL]
                                                        [--out DIR]
                                                        [--width N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Section anchors as they appear in ui/frontend/src/pages/home/.
SECTIONS = [
    ("problem", "#problem"),
    ("how-it-works", "#how-it-works"),
    ("agents", "#agents"),
    ("surgbot", "#surgbot"),
    ("tech", "#tech"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:5173/")
    ap.add_argument("--out", default="/tmp/surgos-snaps")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--only", help="only this section anchor name")
    ap.add_argument("--sel", help="screenshot one arbitrary CSS selector instead of sections")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.goto(args.url, wait_until="networkidle")
        # ReactFlow computes fitView after mount; give it a beat to settle.
        page.wait_for_timeout(1500)

        if args.sel:
            el = page.query_selector(args.sel)
            if el is None:
                print(f"selector not found: {args.sel}")
                return 1
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(900)
            path = out / "selector.png"
            el.screenshot(path=str(path))
            box = el.bounding_box() or {}
            print(f"{path}   {box.get('width',0):.0f}x{box.get('height',0):.0f}")
            browser.close()
            return 0

        full = out / "full-page.png"
        page.screenshot(path=str(full), full_page=True)
        print(f"{full}")

        for name, sel in SECTIONS:
            if args.only and args.only != name:
                continue
            el = page.query_selector(sel)
            if el is None:
                print(f"  (no {sel} on page, skipped)")
                continue
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(600)
            path = out / f"{name}.png"
            el.screenshot(path=str(path))
            box = el.bounding_box() or {}
            print(f"{path}   {box.get('width', 0):.0f}x{box.get('height', 0):.0f}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
