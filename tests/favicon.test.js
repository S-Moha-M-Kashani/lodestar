// tests/favicon.test.js
//
// The browser tab draws the favicon at 16px. The first nova favicon kept the
// header mark's proportions — the glyph spanned ~60% of its 64-unit canvas —
// and at tab size that read as a speck. A favicon has no room for margins:
// the drawing must fill the canvas nearly edge to edge.
//
// Read out of index.html rather than screenshotted: no browser renders its own
// tab for Playwright to look at, but the geometry that decides the size is
// right there in the data URI.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const html = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'index.html'), 'utf8');

// This is a configuration invariant.
test('the tab icon fills its canvas instead of floating in it', () => {
  const icon = html.match(/rel="icon" href="data:image\/svg\+xml,([^"]+)"/);
  assert.ok(icon, 'index.html must carry an inline SVG favicon');
  const svg = decodeURIComponent(icon[1]);

  const viewBox = svg.match(/viewBox="0 0 (\d+) \d+"/);
  assert.ok(viewBox, 'the icon must declare a square viewBox at origin');
  const canvas = Number(viewBox[1]);

  // The glyph's own bounding box, before any transform. Rotation about the
  // centre preserves each point's radius, so pre-transform extent is what
  // bounds the drawn size.
  const points = [...svg.matchAll(/[ML] ?([\d.]+) ([\d.]+)/g)]
    .map(([, x, y]) => [Number(x), Number(y)]);
  assert.ok(points.length >= 4, 'the icon must draw real paths');
  const span = (axis) => Math.max(...points.map((p) => p[axis]))
    - Math.min(...points.map((p) => p[axis]));

  for (const [axis, name] of [[0, 'width'], [1, 'height']]) {
    assert.ok(span(axis) / canvas >= 0.8,
      `the glyph spans ${(span(axis) / canvas * 100).toFixed(0)}% of the `
      + `canvas ${name} — under 80% it renders as a speck at tab size`);
  }
});
