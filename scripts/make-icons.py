#!/usr/bin/env python3
"""Render the home-screen icons from the nova already in index.html.

    uv run --with playwright python scripts/make-icons.py

The mark is authored once, as SVG, in the sprite at the top of index.html —
filters, nebula, cloud mask, star field and the four-spike star. A phone's home
screen cannot take an SVG (iOS ignores SVG for `apple-touch-icon` entirely), so
the icons under icons/ are raster copies of that one drawing, and this script is
the only thing that may write them: hand-drawn icons would drift from the header
mark the first time the mark moved, and nobody would notice, because the two are
never on screen together.

Two deliberate differences from the header mark:

  * No vignette. `#nv-vignette` fades the scene into a disc, which is right on
    paper and wrong on a tile — iOS composites a transparent icon onto black
    and Android crops it to its own shape, so both want the art to reach every
    corner. The icon uses the whole 200 x 200 scene instead.
  * The stars variant, always. The header picks colour-only on the paper themes;
    a home screen has no theme to answer to, and the star field is the richer
    drawing.

The star sits at 51% of the tile, well inside the 80% safe circle an adaptive
icon may crop to, which is what lets one 512px file serve `any maskable`.
"""

import os
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "icons")

# Each icon: file name, pixel size, and how much of the 200-unit scene it shows.
# 180 is what iOS asks for as apple-touch-icon; 192 and 512 are the manifest's.
ICONS = [("lodestar-180.png", 180), ("lodestar-192.png", 192),
         ("lodestar-512.png", 512)]


def sprite():
    """The <svg class="svg-sprite"> block from index.html, verbatim."""
    html = open(os.path.join(ROOT, "index.html")).read()
    start = html.index('<svg width="0" height="0" class="svg-sprite"')
    end = html.index("</svg>", html.index("</defs>")) + len("</svg>")
    return html[start:end]


def page_html(size):
    return f"""<!doctype html><meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; background: #04060f; }}
  #tile {{ width: {size}px; height: {size}px; display: block; }}
</style>
{sprite()}
<svg id="tile" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="#04060f"/>
  <use href="#nv-colour"/>
  <use href="#nv-cloud"/>
  <use href="#nv-field"/>
  <use href="#nv-star"/>
</svg>
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size in ICONS:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(page_html(size))
            page.wait_for_timeout(200)  # the blur filters have to settle
            path = os.path.join(OUT, name)
            page.locator("#tile").screenshot(path=path, omit_background=False)
            page.close()
            print(f"wrote {os.path.relpath(path, ROOT)} ({size}x{size})")
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
