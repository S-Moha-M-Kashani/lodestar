// tests/ports.test.js
//
// Port allocation is a shared resource across four processes plus the local
// Chroma stack. Chat memory moved from an on-disk store to that Chroma server,
// so Chroma is now running whenever a board runs — and any brain that binds one
// of its ports fails to start (or, worse, a board silently proxies agent calls
// into Chroma's REST API and gets nonsense back).
//
// These assertions read the real npm scripts, so a future edit that reintroduces
// a collision fails here instead of at runtime.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (...p) => readFileSync(join(ROOT, ...p), 'utf8');
const scripts = JSON.parse(read('package.json')).scripts;

// Owned by the Chroma stack (docker compose in ~/vectordb-lab):
//   8001 = Chroma REST API, 8002 = the browsing viewer.
// The brain deliberately lives in the 9000 block instead, so it can never
// collide with that stack as it grows. Boards pair with brains by last digit:
//   3000 board <-> 9000 brain,  3001 test board <-> 9001 test brain.
const CHROMA_PORTS = [8001, 8002];
const MAIN_BRAIN_PORT = 9000;
const MAIN_BOARD_PORT = 3000;

const portOf = (script, re) => {
  const m = scripts[script].match(re);
  assert.ok(m, `could not read a port out of the "${script}" script`);
  return Number(m[1]);
};

const testBoardPort = () => portOf('test-board', /(?:^|\s)PORT=(\d+)/);
const testBoardAgentPort = () =>
  portOf('test-board', /AGENT_URL=http:\/\/127\.0\.0\.1:(\d+)/);
const testBrainPort = () => portOf('test-brain', /--port\s+(\d+)/);
const testBrainBoardPort = () =>
  portOf('test-brain', /BOARD_API_URL=http:\/\/127\.0\.0\.1:(\d+)/);

test('the test brain does not bind a port owned by the Chroma stack', () => {
  const port = testBrainPort();
  assert.ok(
    !CHROMA_PORTS.includes(port),
    `test-brain binds :${port}, which the Chroma stack owns ` +
      `(${CHROMA_PORTS.join(', ')}) — uvicorn cannot start while Chroma runs`,
  );
});

test('the test board proxies agent calls to the test brain, not to Chroma', () => {
  const agentPort = testBoardAgentPort();
  assert.ok(
    !CHROMA_PORTS.includes(agentPort),
    `test-board sends agent traffic to :${agentPort}, which is Chroma`,
  );
  assert.equal(
    agentPort,
    testBrainPort(),
    'test-board AGENT_URL must point at the port test-brain actually binds',
  );
});

test('every long-running port is distinct', () => {
  const ports = [
    MAIN_BOARD_PORT,
    MAIN_BRAIN_PORT,
    testBoardPort(),
    testBrainPort(),
    ...CHROMA_PORTS,
  ];
  assert.equal(
    new Set(ports).size,
    ports.length,
    `port collision among [${ports.join(', ')}]`,
  );
});

test('the paired test board and test brain point at each other', () => {
  // The pairing invariant: board-3001.db is served by the brain that was told
  // to write to :3001, so the test board never mutates the real board.
  assert.equal(testBrainBoardPort(), testBoardPort());
  assert.notEqual(testBoardPort(), MAIN_BOARD_PORT);
});

test("the Node proxy's default AGENT_URL matches the brain's port", () => {
  const m = read('server.js').match(
    /AGENT_URL\s*=\s*process\.env\.AGENT_URL\s*\|\|\s*'http:\/\/127\.0\.0\.1:(\d+)'/,
  );
  assert.ok(m, 'could not read the AGENT_URL default out of server.js');
  assert.equal(Number(m[1]), MAIN_BRAIN_PORT);
});

test('the composed brain listens on the port compose dials', () => {
  // Nothing else covers this pair, and it is the one that breaks silently:
  // a container-internal port never collides on the host, so a drift between
  // the Dockerfile and compose only shows up as "assistant unavailable".
  const exposed = read('brain/Dockerfile').match(/--port["\s,]+(\d+)/);
  assert.ok(exposed, 'could not read --port out of brain/Dockerfile CMD');
  const dialled = read('docker-compose.yml').match(
    /AGENT_URL:\s*http:\/\/brain:(\d+)/,
  );
  assert.ok(dialled, 'could not read AGENT_URL out of docker-compose.yml');
  assert.equal(
    Number(dialled[1]),
    Number(exposed[1]),
    'docker-compose dials a port the brain container does not listen on',
  );
});

test('the brain Dockerfile EXPOSEs the port it serves', () => {
  const dockerfile = read('brain/Dockerfile');
  const exposed = dockerfile.match(/EXPOSE\s+(\d+)/);
  const served = dockerfile.match(/--port["\s,]+(\d+)/);
  assert.ok(exposed && served, 'brain/Dockerfile is missing EXPOSE or --port');
  assert.equal(Number(exposed[1]), Number(served[1]));
});
