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

// Owned by the *external* Chroma stack (docker compose in ~/vectordb-lab):
//   8001 = Chroma REST API, 8002 = the browsing viewer.
// The brain deliberately lives in the 9000 block instead, so it can never
// collide with that stack as it grows. Boards pair with brains by last digit:
//   3000 board <-> 9000 brain,  3001 test board <-> 9001 test brain.
const CHROMA_PORTS = [8001, 8002];
// Lodestar's own Chroma (the compose `chroma` service, Session 7): published
// on the host so a native brain reaches the same store the composed one does.
// Next free slot in the 8000 block — both stacks must be able to run at once.
const PROJECT_CHROMA_PORT = 8003;
// The test stack's own Chroma (compose `chroma-test`): a physically separate
// store under databases/test/, so test chunks and vectors never share files
// with real chat memory. Next slot after the real one.
const TEST_CHROMA_PORT = 8004;
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

// This is a configuration invariant.
test('the test brain does not bind a port owned by the Chroma stack', () => {
  const port = testBrainPort();
  assert.ok(
    !CHROMA_PORTS.includes(port),
    `test-brain binds :${port}, which the Chroma stack owns ` +
      `(${CHROMA_PORTS.join(', ')}) — uvicorn cannot start while Chroma runs`,
  );
});

// This is a configuration invariant.
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

// This is a configuration invariant.
test('every long-running port is distinct', () => {
  const ports = [
    MAIN_BOARD_PORT,
    MAIN_BRAIN_PORT,
    testBoardPort(),
    testBrainPort(),
    ...CHROMA_PORTS,
    PROJECT_CHROMA_PORT,
    TEST_CHROMA_PORT,
  ];
  assert.equal(
    new Set(ports).size,
    ports.length,
    `port collision among [${ports.join(', ')}]`,
  );
});

// The project's own Chroma, three ways it can drift: the compose service
// binding a port the ~/vectordb-lab stack owns (one of the two stacks then
// cannot start), the native brain's default dialling a different Chroma than
// the composed one persists to, and `npm run chroma` respelling the service
// instead of delegating to it.
const projectChromaHostPort = () => {
  const block = read('docker-compose.yml').match(
    /\n  chroma:\n([\s\S]*?)(?=\n  \S|\n\S|$)/);
  assert.ok(block, 'docker-compose.yml has no chroma service');
  const m = block[1].match(/"(\d+):(\d+)"/);
  assert.ok(m, 'could not read a port mapping out of the chroma service');
  return Number(m[1]);
};

// This is a configuration invariant.
test("the project Chroma binds :8003, never the vectordb-lab stack's ports", () => {
  const host = projectChromaHostPort();
  assert.equal(host, PROJECT_CHROMA_PORT);
  assert.ok(!CHROMA_PORTS.includes(host),
    `:${host} belongs to ~/vectordb-lab — the two stacks must run at once`);
});

// The test stack's Chroma. Two ways this can drift: the chroma-test service
// binding a port some other service owns, and the test brain quietly dialling
// the *real* Chroma on :8003 — which would put test chunks and vectors back
// into the real store, the exact mixing the second service exists to prevent.
const testChromaHostPort = () => {
  const block = read('docker-compose.yml').match(
    /\n  chroma-test:\n([\s\S]*?)(?=\n  \S|\n\S|$)/);
  assert.ok(block, 'docker-compose.yml has no chroma-test service');
  const m = block[1].match(/"(\d+):(\d+)"/);
  assert.ok(m, 'could not read a port mapping out of the chroma-test service');
  return Number(m[1]);
};

// This is a configuration invariant.
test('the test Chroma binds :8004, colliding with nothing', () => {
  const host = testChromaHostPort();
  assert.equal(host, TEST_CHROMA_PORT);
  assert.ok(!CHROMA_PORTS.includes(host),
    `:${host} belongs to ~/vectordb-lab — the stacks must run at once`);
  assert.notEqual(host, projectChromaHostPort(),
    'the test Chroma must be a separate store, not the real one');
});

// This is a configuration invariant.
test('the test brain dials the test Chroma, never the real store', () => {
  const brain = scripts['test-brain'];
  const m = brain.match(/BRAIN_CHROMA_URL=http:\/\/localhost:(\d+)/);
  assert.ok(m,
    'test-brain must pin BRAIN_CHROMA_URL — without it the brain takes the ' +
      ':8003 default and test chat memory lands in the real store');
  assert.equal(Number(m[1]), testChromaHostPort(),
    'test-brain dials a port the chroma-test service does not publish');
});

// This is a configuration invariant.
test("the brain's native BRAIN_CHROMA_URL default dials the port compose publishes", () => {
  const m = read('brain/src/lodestar_brain/config.py').match(
    /BRAIN_CHROMA_URL', *\n? *'http:\/\/localhost:(\d+)'/);
  assert.ok(m, 'could not read the BRAIN_CHROMA_URL default out of config.py');
  assert.equal(Number(m[1]), projectChromaHostPort(),
    'a native brain and the composed brain would persist to different stores');
});

// This is a configuration invariant.
test('npm run chroma delegates to the compose service instead of respelling it', () => {
  // The service definition carries the port and the bind mount; a second copy
  // in package.json is the copy that drifts (the same rule as `npm run lab`).
  assert.ok(scripts.chroma, 'package.json has no "chroma" script');
  assert.match(scripts.chroma, /docker compose up/);
  assert.match(scripts.chroma, /chroma/);
  assert.ok(!/docker run|-p |--publish|:\d{4}/.test(scripts.chroma),
    'the chroma script respells the port or mount — the compose service is the one definition');
});

// This is a configuration invariant.
test('the paired test board and test brain point at each other', () => {
  // The pairing invariant: board-3001.db is served by the brain that was told
  // to write to :3001, so the test board never mutates the real board.
  assert.equal(testBrainBoardPort(), testBoardPort());
  assert.notEqual(testBoardPort(), MAIN_BOARD_PORT);
});

// This is a configuration invariant.
test('the brain launcher installs the backend its default embedder needs', () => {
  // The same pairing as the lab above, for the same reason: the brain defaults
  // to a Persian-tuned sentence-transformers model, and without the extra it
  // starts happily and fails on the first retrieval call. A script that names
  // its own BRAIN_EMBEDDER is taken at its word — that is the escape hatch for
  // a deliberately light brain, and `fake` needs nothing installed at all.
  const cfg = read('brain/src/lodestar_brain/config.py');
  const chosen = scripts['test-brain'].match(/BRAIN_EMBEDDER=(\S+)/)?.[1]
    ?? cfg.match(/embedder: str = '([^']+)'/)?.[1];
  assert.ok(chosen, 'could not tell which embedder `npm run test-brain` uses');
  const needed = {
    fastembed: 'semantic',
    'sentence-transformers': 'local-embeddings',
  }[chosen];
  if (!needed) return;
  assert.match(
    scripts['test-brain'],
    new RegExp(`--extra\\s+${needed}\\b`),
    `test-brain runs the ${chosen} embedder but never installs the ` +
      `'${needed}' extra it needs`,
  );
});

// This is a configuration invariant.
test("the Node proxy's default AGENT_URL matches the brain's port", () => {
  const m = read('server.js').match(
    /AGENT_URL\s*=\s*process\.env\.AGENT_URL\s*\|\|\s*'http:\/\/127\.0\.0\.1:(\d+)'/,
  );
  assert.ok(m, 'could not read the AGENT_URL default out of server.js');
  assert.equal(Number(m[1]), MAIN_BRAIN_PORT);
});

// This is a configuration invariant.
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

// This is a configuration invariant.
test('the brain Dockerfile EXPOSEs the port it serves', () => {
  const dockerfile = read('brain/Dockerfile');
  const exposed = dockerfile.match(/EXPOSE\s+(\d+)/);
  const served = dockerfile.match(/--port["\s,]+(\d+)/);
  assert.ok(exposed && served, 'brain/Dockerfile is missing EXPOSE or --port');
  assert.equal(Number(exposed[1]), Number(served[1]));
});

// Ollama is one more process, and unlike ours it is not ours: it is installed
// system-wide and owns 11434 by convention. The brain reaches it as a client, so
// the requirement is only that we never bind it. The RAG lab used to be checked
// here too, against the same port for the same reason; it moved to its own
// repository on 2026-08-11 and holds that default itself.
const OLLAMA_PORT = 11434;

// This is a configuration invariant.
test('the local model server is reached, never bound', () => {
  const brain = read('brain/src/lodestar_brain/config.py').match(
    /ollama_base_url: str = 'http:\/\/localhost:(\d+)\/v1'/,
  );
  assert.ok(brain, 'could not read ollama_base_url out of the brain settings');
  assert.equal(Number(brain[1]), OLLAMA_PORT);

  // And it collides with nothing we start ourselves.
  const ours = [
    MAIN_BOARD_PORT,
    MAIN_BRAIN_PORT,
    testBoardPort(),
    testBrainPort(),
    ...CHROMA_PORTS,
  ];
  assert.ok(!ours.includes(OLLAMA_PORT), `we bind :${OLLAMA_PORT} ourselves`);
});

// This is a configuration invariant.
test("the '/v1' is part of the URL, not appended by a caller", () => {
  // Ollama's OpenAI-compatible surface lives under /v1, and the model builder
  // takes the base URL verbatim. Keeping the suffix in the setting is what lets
  // the same field point at llama.cpp or vLLM without a code change — and stops
  // a caller from inventing a second convention about who adds it.
  //
  // Asserted as "the setting is used, and never concatenated onto" rather than
  // as one exact call shape: the two backends were collapsed to a single
  // init_chat_model site, and a test pinned to the old spelling would have
  // failed for a refactor that cannot violate the rule it exists to protect.
  const brain = read('brain/src/lodestar_brain/config.py');
  assert.match(brain, /BRAIN_OLLAMA_BASE_URL', *\n? *'http:\/\/localhost:11434\/v1'/);
  const factory = read('brain/src/lodestar_brain/llm.py');
  assert.match(factory, /settings\.ollama_base_url/);
  assert.ok(
    !/ollama_base_url\s*\+/.test(factory),
    'the factory is concatenating onto the base URL instead of using it',
  );
});
