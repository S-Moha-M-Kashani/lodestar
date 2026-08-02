// scripts/lab.mjs — one command: prove the lab, then run it.
//
// The RAG Lab's whole claim is that retrieval choices here were decided by
// measurement. That claim is worth exactly as much as the suite behind it, so
// this runner refuses to open the panel on code whose tests do not pass: a
// green terminal above the URL is the point, not a convenience.
//
// It deliberately does *not* spell the uvicorn line. `npm run raglab` already
// carries the four `--with` pins and the `--extra` that the configured default
// embedder needs, and tests/ports.test.js enforces that pairing. Copying it
// here would be a second place to keep in step, and the copy is the one that
// would go stale.
import { spawn, spawnSync } from 'node:child_process';
import { createConnection } from 'node:net';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const argv = process.argv.slice(2);
const has = (flag) => argv.includes(flag);

if (has('--help') || has('-h')) {
  console.log(`Usage: npm run lab [-- <options>]

Runs the RAG lab's tests, then starts the lab on the port npm run raglab binds.

  --no-test   start the lab without running the suite first
  --all       run the whole brain suite, not just the lab's
  --test-only run the suite and stop
`);
  process.exit(0);
}

const scripts = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).scripts;
const PORT = Number(scripts.raglab.match(/--port\s+(\d+)/)?.[1]);

const run = (command, args, extra = {}) =>
  spawnSync(command, args, { cwd: ROOT, stdio: 'inherit', ...extra }).status;

const free = (port) => new Promise((resolve) => {
  const probe = createConnection({ port, host: '127.0.0.1' })
    .on('connect', () => { probe.destroy(); resolve(false); })
    .on('error', () => resolve(true));
});

// ---------------------------------------------------------------------------
// 1. the suite
// ---------------------------------------------------------------------------
if (!has('--no-test')) {
  // The lab's own file by default. The full brain suite is a minute rather
  // than seconds, and the question this runner answers — "is the lab sound
  // enough to look at?" — is answered by the lab's tests.
  const target = has('--all') ? 'brain/tests' : 'brain/tests/test_raglab.py';
  console.log(`\n\x1b[1m▸ ${target}\x1b[0m`);
  const status = run('uv', ['run', '--project', 'brain', 'pytest', target, '-q']);
  if (status !== 0) {
    console.error('\n\x1b[31m✗ tests failed — not starting the lab.\x1b[0m');
    console.error('  Start it anyway with:  npm run lab -- --no-test\n');
    process.exit(status ?? 1);
  }
  console.log('\x1b[32m✓ tests pass\x1b[0m');
}
if (has('--test-only')) process.exit(0);

// ---------------------------------------------------------------------------
// 2. the lab
// ---------------------------------------------------------------------------
if (!(await free(PORT))) {
  // Not an error worth failing on: the usual cause is a lab already running,
  // and the useful thing to print is where it is.
  console.log(`\n\x1b[33m:${PORT} is already in use — a lab is likely already up.\x1b[0m`);
  console.log(`  Panel:  http://localhost:${PORT}/\n`);
  process.exit(0);
}

console.log(`\n\x1b[1m▸ starting the RAG lab\x1b[0m`);
console.log(`  Panel:      http://localhost:${PORT}/`);
console.log(`  In-board:   run npm start too, then Assistant → RAG lab`);
console.log(`  First build downloads the embedding model (~2.2 GB) on first`);
console.log(`  retrieval, not at boot.\n`);

const lab = spawn('npm', ['run', 'raglab'], { cwd: ROOT, stdio: 'inherit' });
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => lab.kill(signal));
}
lab.on('exit', (code, signal) => process.exit(signal ? 0 : code ?? 0));
