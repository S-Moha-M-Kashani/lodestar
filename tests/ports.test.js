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

// The RAG Lab (brain/tests/raglab) is a test-only workbench reached from a page
// inside the board. It is a *third* service in the 9000 block, not a frontend:
// the boards own 3000/3001, so a lab that bound 3001 would take the test board's
// port and there would be no platform left to open the lab page from. What must
// also never drift is which data it can reach — a lab that rebuilds collections
// dozens of times per sweep, pointed at the production Chroma database, would
// delete real chat memory.
const RAGLAB_PORT = 9002;
const raglabPort = () => portOf('raglab', /--port\s+(\d+)/);

test('the RAG lab lives in the 9000 block, not on a board port', () => {
  const port = raglabPort();
  assert.equal(port, RAGLAB_PORT);
  assert.notEqual(port, MAIN_BOARD_PORT);
  assert.notEqual(port, testBoardPort(),
    'the test board needs :3001 — the lab page is served from it');
});

test('the RAG lab never binds a port owned by Chroma or a brain', () => {
  const port = raglabPort();
  assert.ok(!CHROMA_PORTS.includes(port), `raglab binds :${port}, Chroma's port`);
  assert.notEqual(port, MAIN_BRAIN_PORT);
  assert.notEqual(port, testBrainPort());
});

test('the RAG lab launcher installs the backend its default embedder needs', () => {
  // The lab now defaults to a Persian-tuned sentence-transformers model. Without
  // the local-embeddings extra the service starts happily and then fails on the
  // first index build — which reads as "the lab is broken", not "install this".
  const cfg = read('brain/tests/raglab/config.py');
  const chosen = cfg.match(/embedder: str = '([^']+)'/);
  assert.ok(chosen, 'could not read the default embedder out of the lab config');
  const needed = {
    fastembed: 'semantic',
    'sentence-transformers': 'local-embeddings',
  }[chosen[1]];
  if (!needed) return;                        // a hash embedder needs nothing
  assert.match(scripts.raglab, new RegExp(`--extra\\s+${needed}\\b`));
});

test("the Node proxy's default RAGLAB_URL matches the port the lab binds", () => {
  const m = read('server.js').match(
    /RAGLAB_URL\s*=\s*process\.env\.RAGLAB_URL\s*\|\|\s*'http:\/\/127\.0\.0\.1:(\d+)'/,
  );
  assert.ok(m, 'could not read the RAGLAB_URL default out of server.js');
  assert.equal(Number(m[1]), raglabPort());
});

test('the e2e suite pins RAGLAB_URL at a port it never starts', () => {
  // The suite checks the "lab is not running" panel, so it must not inherit
  // server.js's :9002 default: on a machine with the real lab up, the proxy
  // reaches it and three checks go red — green in CI, red for whoever is
  // actually working on the lab, which is the worst way round.
  const e2e = read('tests/e2e_test.py');
  const pin = e2e.match(/"RAGLAB_URL":\s*f"http:\/\/127\.0\.0\.1:\{RAGLAB_PORT\}"/);
  assert.ok(pin, 'tests/e2e_test.py must pass RAGLAB_URL to the Node server');
  const port = e2e.match(/RAGLAB_PORT\s*=\s*int\(os\.environ\.get\("TEST_RAGLAB_PORT",\s*"(\d+)"\)\)/);
  assert.ok(port, 'could not read RAGLAB_PORT out of tests/e2e_test.py');
  assert.notEqual(Number(port[1]), raglabPort(),
    'the e2e proxy port must differ from the port a real lab binds');
});

test('lab traffic and assistant traffic go to different upstreams', () => {
  // One prefix routed to the wrong service is a silent 404 that reads as "the
  // lab is broken", so the split is asserted rather than trusted.
  const server = read('server.js');
  assert.match(server, /\/api\/raglab\//);
  assert.match(server, /RAG lab unavailable/);
  assert.match(server, /assistant unavailable/);
});

test('no RAG lab command names a vector database', () => {
  // The lab's experiments are ephemeral: the index is process memory and the
  // only thing written down is the JSON run. So there is no database to pin —
  // and pinning one again would be the persistence coming back, one typo away
  // from the chat memory a sweep would rebuild forty times.
  for (const [name, script] of Object.entries(scripts)) {
    if (!name.startsWith('raglab')) continue;
    assert.ok(
      !/CHROMA/.test(script),
      `the "${name}" script still names a Chroma database: ${script}`,
    );
  }
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

// Ollama is a *fifth* process, and unlike the other four it is not ours: it is
// installed system-wide and owns 11434 by convention. The brain and the lab both
// reach it as clients, so the requirement is only that we never bind it — and
// that both defaults name the same port, since a lab pointed at a port nothing
// serves fails as "LLM metrics unavailable", which reads like a missing key.
const OLLAMA_PORT = 11434;

test('the local model server is reached, never bound', () => {
  const brain = read('brain/src/lodestar_brain/config.py').match(
    /ollama_base_url: str = 'http:\/\/localhost:(\d+)\/v1'/,
  );
  assert.ok(brain, 'could not read ollama_base_url out of the brain settings');
  assert.equal(Number(brain[1]), OLLAMA_PORT);

  const lab = read('brain/tests/raglab/config.py').match(
    /ollama_base_url: str = 'http:\/\/localhost:(\d+)\/v1'/,
  );
  assert.ok(lab, 'could not read ollama_base_url out of the lab settings');
  assert.equal(Number(lab[1]), Number(brain[1]));

  // And it collides with nothing we start ourselves.
  const ours = [
    MAIN_BOARD_PORT,
    MAIN_BRAIN_PORT,
    RAGLAB_PORT,
    testBoardPort(),
    testBrainPort(),
    ...CHROMA_PORTS,
  ];
  assert.ok(!ours.includes(OLLAMA_PORT), `we bind :${OLLAMA_PORT} ourselves`);
});

test("the '/v1' is part of the URL, not appended by a caller", () => {
  // Ollama's OpenAI-compatible surface lives under /v1, and ChatOpenAI takes the
  // base URL verbatim. Keeping the suffix in the setting is what lets the same
  // field point at llama.cpp or vLLM without a code change — and stops a caller
  // from inventing a second convention about who adds it.
  const brain = read('brain/src/lodestar_brain/config.py');
  assert.match(brain, /BRAIN_OLLAMA_BASE_URL', *\n? *'http:\/\/localhost:11434\/v1'/);
  const factory = read('brain/src/lodestar_brain/llm/factory.py');
  assert.match(factory, /base_url=settings\.ollama_base_url/);
  assert.ok(
    !/ollama_base_url\s*\+/.test(factory),
    'the factory is concatenating onto the base URL instead of using it',
  );
});
