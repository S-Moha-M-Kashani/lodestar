// tests/frontend.test.js
//
// The frontend is a graph of native ES modules under js/, loaded by the browser
// with no bundler in between. That buys real file boundaries at zero build
// cost, but it moves two failures from "the bundler shouts at you" to "the page
// is silently dead", and neither is caught by any other suite here:
//
//   1. A bad import path. There is no resolver step to fail — the browser asks
//      the server for the file, gets a 404, and the whole graph stops. The
//      board renders nothing and the console holds the only clue.
//
//   2. A module the server does not serve. server.js keeps a whitelist rather
//      than mapping request paths onto the filesystem (deliberately — see the
//      note there), so a new module is reachable only if the whitelist knows
//      about it. It is built by walking js/ at boot precisely so this cannot
//      drift, and this file is what proves the walk actually covers the graph.
//
// Both are checked against the real import graph, walked from js/main.js — the
// single entry point index.html loads.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startServer } from './helpers/server-harness.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const JS = join(ROOT, 'js');

/** Every `from '...'` and side-effect `import '...'` in one module. */
function importsOf(file) {
  const src = readFileSync(file, 'utf8');
  return [...src.matchAll(/^import\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]/gm)].map((m) => m[1]);
}

/** Walk the graph from main.js, resolving each specifier as the browser would.
 *  Returns the reached files and any specifier that resolves to nothing. */
function walk() {
  const entry = join(JS, 'main.js');
  const reached = new Set([entry]);
  const broken = [];
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop();
    for (const spec of importsOf(file)) {
      // Bare specifiers would need an import map; the app deliberately has none.
      assert.ok(spec.startsWith('.'), `${relative(ROOT, file)} imports a bare specifier '${spec}'`);
      const target = resolve(dirname(file), spec);
      let ok = true;
      try { readFileSync(target); } catch { ok = false; }
      if (!ok) { broken.push(`${relative(ROOT, file)} -> ${spec}`); continue; }
      if (!reached.has(target)) { reached.add(target); queue.push(target); }
    }
  }
  return { reached, broken };
}

function allModules(dir = JS) {
  const out = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...allModules(p));
    else if (e.name.endsWith('.js')) out.push(p);
  }
  return out;
}

// This is a unit test.
test('every import resolves, and every module is reached from main.js', () => {
  const { reached, broken } = walk();
  assert.deepEqual(broken, [], 'these imports point at files that do not exist');

  // An orphan is not merely dead weight. Several modules wire their own
  // controls as they evaluate (the card dialog, the toolbar, the export
  // sheet), so a module that nothing imports is a screen whose buttons quietly
  // do nothing — main.js names those as side-effect imports for this reason.
  const orphans = allModules()
    .filter((f) => !reached.has(f))
    .map((f) => relative(ROOT, f));
  assert.deepEqual(orphans, [],
    'unreachable from js/main.js — import it, or delete it');
});

// This is an integration test.
test('the server serves every module in the graph as JavaScript', async () => {
  const { reached } = walk();
  const s = await startServer();
  try {
    for (const file of reached) {
      const url = s.base + '/' + relative(ROOT, file).replaceAll('\\', '/');
      const res = await fetch(url);
      assert.equal(res.status, 200, `${url} is in the import graph but not served`);
      // A wrong content type is as fatal as a 404: browsers refuse to execute a
      // module served as text/plain, and refuse it silently.
      assert.match(res.headers.get('content-type') || '', /javascript/, `${url} content-type`);
    }
    // The whitelist is built by walking a directory, so prove it did not turn
    // into a filesystem read. Two shapes, because they test different things:
    // fetch normalises `/js/../server.js` to `/server.js` before it is sent, so
    // that one only asserts the repo's own files are not served; the encoded
    // form arrives at the server intact and is the actual traversal attempt.
    for (const path of ['/js/../server.js', '/js/%2e%2e/server.js', '/js/main.js/../../server.js']) {
      const res = await fetch(s.base + path);
      assert.equal(res.status, 404, `${path} must not be served`);
    }
  } finally { await s.stop(); }
});

// This is a unit test.
//
// The two shells the Assistant now has are each reached from exactly one
// place, and neither is imported by anything the other tests would notice: the
// widget is wired into render(), the shared chrome only into the two shells.
// The orphan check above would catch a module nothing imports, but not the
// reverse mistake — a shell deleted, or quietly folded back into sheet.js,
// leaving the widget's markup in index.html with nothing driving it. Naming
// them here is what makes that a failing test rather than a dead launcher.
test('both assistant shells are reachable from main.js', () => {
  const { reached } = walk();
  const have = new Set([...reached].map((f) => relative(ROOT, f)));
  for (const mod of ['js/assistant/widget.js', 'js/assistant/chrome.js']) {
    assert.ok(have.has(mod), `${mod} is not reachable from js/main.js`);
  }
});
