// tests/compose.test.js
//
// :3000 is served by the composed container, and the Dockerfile bakes the source
// in with `COPY . .`. Nothing rebuilds the image when a static file changes, so a
// container started from an old image keeps serving the frontend as it was on
// build day — silently, because the API and the database are current. That is how
// the Areas and Review views, plus voice input, were missing from :3000 for five
// days while :3001 (plain `node server.js` over the working tree) had them all.
//
// The fix is to mount the source tree over the image's app directory, so the
// container always serves the working tree. The whole tree rather than individual
// files on purpose: a single-file bind mount is pinned to the host file's inode,
// so an editor that saves by write-then-rename leaves the container serving the
// old inode until it restarts — the same drift, quieter.
//
// These assertions read the real compose file, Dockerfile, and server.js, so a
// future edit that reintroduces the drift fails here instead of on :3000.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { resolveBoardDb, resolveAssistantDb } from '../scripts/db-location.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (...p) => readFileSync(join(ROOT, ...p), 'utf8');

// Every file server.js hands the browser, read out of its STATIC route table so
// this list can never fall behind the server.
function servedFiles() {
  const table = read('server.js').match(/const STATIC = \{([\s\S]*?)\n\};/);
  assert.ok(table, 'could not read the STATIC route table out of server.js');
  const files = [...table[1].matchAll(/\[\s*'([^']+)'\s*,/g)].map((m) => m[1]);
  assert.ok(files.length > 0, 'the STATIC route table yielded no files');
  return [...new Set(files)];
}

// The compose file is small and hand-written; pull one service's block out of it
// by indentation rather than taking a YAML dependency (the repo has none).
function serviceBlock(name) {
  const block = read('docker-compose.yml').match(
    new RegExp(`\\n  ${name}:\\n([\\s\\S]*?)(?=\\n  \\S|\\n\\S|$)`),
  );
  assert.ok(block, `could not find the "${name}" service in docker-compose.yml`);
  return block[1];
}

const workdir = () => {
  const m = read('Dockerfile').match(/^WORKDIR\s+(\S+)/m);
  assert.ok(m, 'could not read WORKDIR out of the Dockerfile');
  return m[1];
};

const mountsOf = (block) =>
  [...block.matchAll(/^\s*-\s*(\S+:\S+)\s*$/gm)].map((m) => m[1]);

// The mount that puts the working tree where the server reads it from.
function sourceMount() {
  const dir = workdir();
  const mounts = mountsOf(serviceBlock('lodestar'));
  return mounts.find((m) => /^\.\/?:/.test(m) && m.split(':')[1] === dir);
}

// This is a configuration invariant.
test('compose mounts the source tree over the image app directory', () => {
  const dir = workdir();
  assert.ok(
    sourceMount(),
    `docker-compose.yml does not mount the source tree at ${dir}, so :3000 ` +
      `serves whatever was baked into the image — a frontend change needs a ` +
      `rebuild to appear, and nothing warns when it has not had one. ` +
      `Expected a volume ".:${dir}:ro", got ` +
      `[${mountsOf(serviceBlock('lodestar')).join(', ')}]`,
  );
});

// This is a configuration invariant.
test('the source mount is read-only', () => {
  // The container must never be able to write back over the working tree.
  const mount = sourceMount();
  assert.ok(mount, 'the source tree is not mounted at all');
  assert.ok(
    mount.endsWith(':ro'),
    `the source tree is mounted writable ("${mount}") — the container could ` +
      `overwrite the files it is served from`,
  );
});

// This is a configuration invariant.
test('every file the server serves is inside the mounted tree', () => {
  // Keeps the mount honest as the server grows: a served file that lives outside
  // the repo root would not be covered by mounting the root.
  assert.ok(sourceMount(), 'the source tree is not mounted at all');
  for (const file of servedFiles()) {
    assert.ok(
      !file.startsWith('..') && !file.startsWith('/'),
      `server.js serves "${file}" from outside the mounted tree`,
    );
    assert.ok(
      existsSync(join(ROOT, file)),
      `server.js serves "${file}" but it does not exist at the repo root, so ` +
        `mounting the root does not put it in the container`,
    );
  }
});

// This is a configuration invariant.
test('the container and a native npm start open the same two database files', () => {
  // The 2026-08-30 defect: BOARD_DB pointed into a Docker-owned volume while
  // `npm start` opened databases/real/board.db, so :3000 served whichever
  // stack was started last. The browser's cache refilled the cards and could
  // not refill the boards, so two of them were simply gone.
  const block = serviceBlock('lodestar');
  const mounts = mountsOf(block);

  assert.ok(!mounts.includes('board-data:/data'),
    'the container still has its own private board volume');
  assert.ok(!/\nvolumes:\n[\s\S]*?^ {2}board-data:/m.test(read('docker-compose.yml')),
    'board-data is still declared — a later edit could mount it again');

  // The data mount must be writable: the tree mount is :ro, and a board.db
  // reached only through that one could not be opened for writing at all.
  const data = mounts.find((m) => m.split(':')[1] === '/data');
  assert.ok(data, `nothing is mounted at /data, got [${mounts.join(', ')}]`);
  assert.ok(!data.endsWith(':ro'), `the data mount "${data}" is read-only`);

  // The real invariant, asked of the path resolver rather than of a string:
  // whatever the container opens must be the same host file `npm start` opens.
  //
  // The resolver is run against an EMPTY temporary root, never against this
  // repo. Asked about a repo where databases/real/board.db is absent but a
  // legacy databases/board.db or root-level board.db is present, it does not
  // answer — it PERFORMS the one-time move, backup and renameSync included.
  // A configuration-invariant test that reads two text files must not migrate
  // anybody's database. Both sides are then compared repo-relative, so the
  // question being asked is unchanged: the resolver still names the path, and
  // no literal appears here.
  const hostDir = join(ROOT, data.split(':')[0]);
  const scratch = mkdtempSync(join(tmpdir(), 'lodestar-compose-'));
  try {
    for (const [key, resolve] of [['BOARD_DB', resolveBoardDb],
      ['ASSISTANT_DB', resolveAssistantDb]]) {
      const m = block.match(new RegExp(`^\\s*${key}:\\s*(\\S+)\\s*$`, 'm'));
      assert.ok(m, `the service sets no ${key}, so it falls back to a path ` +
        `inside the read-only tree mount`);
      const inContainer = m[1];
      assert.ok(inContainer.startsWith('/data/'),
        `${key} is "${inContainer}", which is not on the data mount`);
      assert.equal(
        relative(ROOT, join(hostDir, inContainer.slice('/data/'.length))),
        relative(scratch, resolve({ root: scratch, env: {} })),
        `${key} does not resolve to the file a native npm start opens`);
    }
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});

// The brain has the same bake-in drift, but cannot take the same whole-tree
// mount: its virtualenv lives at <workdir>/.venv inside the image, and mounting
// the host's brain/ over the workdir would replace a Linux venv with the host's
// macOS one, so the service would not start. It mounts only its package source,
// which works because uv installs the project editable — inside the container
// lodestar_brain resolves to <workdir>/src/lodestar_brain.
const brainWorkdir = () => {
  const m = read('brain/Dockerfile').match(/^WORKDIR\s+(\S+)/m);
  assert.ok(m, 'could not read WORKDIR out of brain/Dockerfile');
  return m[1];
};

// The directory the brain Dockerfile copies its source into, e.g. `COPY src ./src`.
const brainSourceDir = () => {
  const m = read('brain/Dockerfile').match(/^COPY\s+(\S+)\s+\.\/(\S+)\s*$/m);
  assert.ok(m, 'could not read the source COPY out of brain/Dockerfile');
  return { host: m[1], target: m[2] };
};

const brainSourceMount = () => {
  const { host, target } = brainSourceDir();
  return mountsOf(serviceBlock('brain')).find(
    (m) => m.startsWith(`./brain/${host}:`) &&
      m.split(':')[1] === `${brainWorkdir()}/${target}`,
  );
};

// This is a configuration invariant.
test('compose mounts the brain source over the image copy', () => {
  const { host, target } = brainSourceDir();
  assert.ok(
    brainSourceMount(),
    `docker-compose.yml does not mount the brain source, so the composed brain ` +
      `runs whatever Python was baked into the image — a brain change needs a ` +
      `rebuild to take effect, and nothing warns when it has not had one. ` +
      `Expected a volume "./brain/${host}:${brainWorkdir()}/${target}:ro", got ` +
      `[${mountsOf(serviceBlock('brain')).join(', ')}]`,
  );
});

// The brain used to choose its embedder and transcriber with an 'auto' mode that
// probed for an optional wheel and quietly took whatever it found. That made the
// composed brain's behaviour depend on the image contents rather than on config:
// brain/Dockerfile installs `--extra semantic` but never `--extra voice` (mlx is
// Apple-Silicon only), so Docker got fastembed and OpenRouter dictation while a
// Mac got hash and Parakeet — from identical settings. With 'auto' gone the image
// has no fallback to hide behind, so compose has to name both backends outright.
const brainEnvPin = (key) =>
  serviceBlock('brain').match(new RegExp(`^\\s*${key}:\\s*(.+)$`, 'm'))?.[1].trim();

// Which extra each model-backed embedder needs, and what the brain image
// actually installs. A pin the image cannot load is the same bug as no pin at
// all, just later: the container starts, answers /health, and fails on the
// first retrieval call.
const EMBEDDER_EXTRA = {
  fastembed: 'semantic',
  'sentence-transformers': 'local-embeddings',
};
const brainExtras = () =>
  new Set([...read('brain/Dockerfile').matchAll(/--extra\s+(\S+)/g)].map((m) => m[1]));

// This is a configuration invariant.
test('compose pins the brain embedder to the one its image installs', () => {
  const pinned = brainEnvPin('BRAIN_EMBEDDER');
  assert.ok(
    pinned && pinned.includes('sentence-transformers'),
    `the composed brain does not pin BRAIN_EMBEDDER to sentence-transformers ` +
      `(got ${pinned ?? 'nothing'}). It is the measured winner on the Farsi ` +
      `corpus — 0.617 recall against fastembed's Latin-only ~0.01, a ~60x gap ` +
      `where every other knob in the sweep was worth under 2%. Pinning anything ` +
      `else leaves the container embedding with a model that cannot read the ` +
      `corpus while the docs name one that can, and nothing fails to say so.`,
  );
  const needed = EMBEDDER_EXTRA[pinned.trim()];
  assert.ok(
    !needed || brainExtras().has(needed),
    `compose pins BRAIN_EMBEDDER=${pinned}, which needs the '${needed}' extra, ` +
      `but brain/Dockerfile installs [${[...brainExtras()].join(', ')}]. The ` +
      `container would start and then fail on its first retrieval call.`,
  );
});

// This is a configuration invariant.
test('compose pins the brain transcriber away from Parakeet', () => {
  const pinned = brainEnvPin('BRAIN_TRANSCRIBER');
  assert.ok(
    pinned && pinned.includes('openrouter'),
    `the composed brain does not pin BRAIN_TRANSCRIBER to openrouter (got ` +
      `${pinned ?? 'nothing'}). parakeet-mlx is Apple-Silicon only and the brain ` +
      `image never installs the 'voice' extra, so any other value leaves the ` +
      `container unable to transcribe at all.`,
  );
});

// This is a configuration invariant.
test('the brain source mount is read-only', () => {
  const mount = brainSourceMount();
  assert.ok(mount, 'the brain source is not mounted at all');
  assert.ok(
    mount.endsWith(':ro'),
    `the brain source is mounted writable ("${mount}") — the container could ` +
      `overwrite the files it imports from`,
  );
});

// This is a configuration invariant.
test('no brain mount shadows the container virtualenv', () => {
  // The trap this guards: the host's brain/.venv holds macOS binaries, so a
  // mount over the workdir (or straight onto .venv) hides the image's Linux
  // venv and the brain stops starting at all.
  const dir = brainWorkdir();
  for (const mount of mountsOf(serviceBlock('brain'))) {
    const target = mount.split(':')[1];
    assert.notEqual(
      target,
      dir,
      `mount "${mount}" covers the whole workdir ${dir}, shadowing ${dir}/.venv ` +
        `with the host virtualenv — the brain will not start`,
    );
    assert.notEqual(
      target,
      `${dir}/.venv`,
      `mount "${mount}" replaces the container virtualenv with the host's`,
    );
  }
});

// --- The project's own Chroma (Session 7, stage 3) --------------------------
// Chat-memory chunks and their vectors are derived data (rebuilt from
// assistant.db) and live beside the other databases, in databases/real/.
// A bind mount rather than a named volume deliberately: "where is my data" has
// one answer — the databases/ folder — and stage 1's backup exclusion of
// the chroma folders actually covers it.

// This is a configuration invariant.
test('compose runs the project Chroma over databases/real/chroma-data', () => {
  const block = serviceBlock('chroma');
  assert.ok(
    mountsOf(block).some((m) => m.startsWith('./databases/real/chroma-data:')),
    `the chroma service must bind-mount ./databases/real/chroma-data — a named ` +
      `volume would move the derived store back out of the databases/ folder, ` +
      `and the pre-split ./databases/chroma-data would sit beside the test ` +
      `data again. Got [${mountsOf(block).join(', ')}]`,
  );
  assert.match(block, /IS_PERSISTENT/,
    'chroma persistence must be pinned on, or the store silently becomes per-boot');
});

// This is a configuration invariant.
test('the test Chroma persists to databases/test, physically apart from the real store', () => {
  // The :3001 stack's chunks and vectors get their own files, not a logical
  // database inside the real store: a separate service over a separate bind
  // mount, so "delete the test data" is `rm -rf databases/test` and can never
  // touch real chat memory.
  const block = serviceBlock('chroma-test');
  assert.ok(
    mountsOf(block).some((m) => m.startsWith('./databases/test/chroma-data-3001:')),
    `the chroma-test service must bind-mount ./databases/test/chroma-data-3001. ` +
      `Got [${mountsOf(block).join(', ')}]`,
  );
  assert.match(block, /IS_PERSISTENT/,
    'the test store is the durable :3001 sandbox — persistence pinned on, like the real one');
});

// This is a configuration invariant.
test('the composed brain dials the project Chroma service, and only after it starts', () => {
  // The host side carries the loopback prefix now (see the publication test
  // below); the container port is what this test is about.
  const mapping = serviceBlock('chroma').match(/"(?:127\.0\.0\.1:)?(\d+):(\d+)"/);
  assert.ok(mapping, 'could not read a port mapping out of the chroma service');
  const pinned = brainEnvPin('BRAIN_CHROMA_URL');
  assert.ok(
    pinned && pinned.includes(`http://chroma:${mapping[2]}`),
    `BRAIN_CHROMA_URL is ${pinned ?? 'unset'} — the composed brain must dial ` +
      `the chroma service on its container port ${mapping[2]}, not a host URL`,
  );
  // Start order matters more than usual here: a brain that boots first
  // disables chat memory until its next restart.
  assert.match(serviceBlock('brain'), /depends_on:[^-]*-\s*chroma/,
    'the brain service must depends_on chroma');
});

// This is a configuration invariant.
test('the composed brain can boot without a Safe Browsing key', () => {
  // BRAIN_URL_SAFETY's *env* default is the real backend, which raises at boot
  // with no key. That is deliberate for a native run — a link check that
  // silently never fires is worse than none — but it would turn
  // `docker compose up --build` into a crash loop for anyone who has no key. So
  // compose states the choice instead of inheriting it.
  const pinned = brainEnvPin('BRAIN_URL_SAFETY');
  assert.ok(pinned, 'the composed brain must pin BRAIN_URL_SAFETY, or it cannot boot keyless');
  assert.match(pinned, /off|google-safe-browsing|fake/,
    `BRAIN_URL_SAFETY is ${pinned}, which is not a backend safety.py knows`);
});

// This is a configuration invariant.
test('the composed brain can boot without a LangSmith key', () => {
  // Same shape as the Safe Browsing pin above, and the same reason: BRAIN_TRACING's
  // env default is 'langsmith', which raises at boot with no key. Unpinned, a
  // keyless `docker compose up --build` would crash-loop. The pin also keeps a
  // container from shipping conversations to a third-party cloud by default.
  const pinned = brainEnvPin('BRAIN_TRACING');
  assert.ok(pinned, 'the composed brain must pin BRAIN_TRACING, or it cannot boot keyless');
  assert.match(pinned, /off|langsmith/,
    `BRAIN_TRACING is ${pinned}, which is not a backend tracing.py knows`);
});

// This is a configuration invariant.
test('the source mount targets exactly where the Dockerfile puts the source', () => {
  // A WORKDIR change would otherwise leave the tree mounted in a directory the
  // server does not run from, and :3000 would go back to serving stale assets
  // with nothing failing.
  const dir = workdir();
  const mount = sourceMount();
  assert.ok(mount, `nothing is mounted at the Dockerfile WORKDIR ${dir}`);
  assert.equal(mount.split(':')[1], dir);
});

// --- The vector store's version (Session 11) --------------------------------
// `chromadb/chroma:latest` is a pin in name only: the tag moves, so a rebuild or
// a `docker compose pull` can hand the real store a different Chroma than the
// one that wrote it — the external review's one uncontested infrastructure
// point. Measured on this machine on 2026-08-18: the cached image was `latest`
// as of 2026-05-05 (`chromadb/chroma@sha256:1e0b73a1…`, whose own binary reports
// `chroma 1.4.4`), while `latest` in the registry already resolved to
// `sha256:abcce7c3…` — the drift had happened and nothing said so.
//
// An exact tag (`chromadb/chroma:1.5.9`) and a digest (`chromadb/chroma@sha256:…`)
// both satisfy this test, because both are immutable; `latest` never does. The
// two services must name the same one — the test twin exists to rehearse what
// the real store will do, and it cannot do that on another version.
const chromaImage = (name) =>
  serviceBlock(name).match(/^\s*image:\s*(\S+)\s*$/m)?.[1];

// This is a configuration invariant.
test('both chroma services pin one exact image version, never latest', () => {
  const images = ['chroma', 'chroma-test'].map(chromaImage);
  for (const [i, image] of images.entries()) {
    const service = ['chroma', 'chroma-test'][i];
    assert.ok(image, `the ${service} service names no image at all`);
    assert.doesNotMatch(
      image,
      /:latest$|^chromadb\/chroma$/,
      `the ${service} service runs "${image}" — a moving tag, so the vector ` +
        `store's version is whatever the registry served on build day. Name an ` +
        `exact version (chromadb/chroma:<x.y.z>) or a digest ` +
        `(chromadb/chroma@sha256:<digest>) instead.`,
    );
  }
  assert.equal(
    images[0],
    images[1],
    `chroma runs "${images[0]}" and chroma-test runs "${images[1]}" — the test ` +
      `twin rehearses what the real store will do, which it cannot do on a ` +
      `different version`,
  );
  // No third service running the image, which would be a third thing to drift.
  // Declarations only: the comment above the pin names `chromadb/chroma:latest`
  // in the command that re-establishes the version, and prose cannot drift.
  assert.equal(
    [...read('docker-compose.yml').matchAll(/^\s*image:\s*chromadb\/chroma\S*/gm)]
      .length,
    2,
    'a service other than chroma and chroma-test runs the chroma image — ' +
      'every declaration has to be pinned, or that one moves',
  );
});

// This is a configuration invariant. Docker publishes a port by writing its
// own nat rules, which is why a bare "3000:3000" is not merely "the default":
// it opens the port on every interface AND routes around the host firewall, so
// the board answered every peer on university Wi-Fi through a machine whose
// firewall was on. The loopback prefix is the whole boundary for the composed
// stack, since the app inside the container must bind 0.0.0.0 for Docker to
// reach it at all.
test('every published host port binds loopback, and none is bare', () => {
  const yaml = read('docker-compose.yml');
  // Lines under a `ports:` key, ignoring comments — i.e. actual mappings.
  const mappings = [...yaml.matchAll(/^\s+- "([^"]+)"$/gm)]
    .map((m) => m[1])
    .filter((v) => /^[\d.:]+$/.test(v));
  assert.ok(mappings.length >= 3,
    `expected the three published services, found ${mappings.length}`);
  for (const mapping of mappings) {
    assert.ok(mapping.startsWith('127.0.0.1:'),
      `"${mapping}" publishes on every interface — prefix it with 127.0.0.1:`);
    // host:container, both present: "127.0.0.1:3000" alone would publish a
    // random host port, which is a different bug wearing the same prefix.
    assert.equal(mapping.split(':').length, 3, `"${mapping}" is not host:container`);
  }
});

// This is a configuration invariant. The container is the one place a loopback
// bind is wrong — Docker forwards to the container's own address — so the
// override and the Host allowlist entry travel together with the compose-only
// hostname the brain dials. Miss either and the stack starts and then refuses
// every request it receives, which reads as "the brain is down".
test('the composed board binds every interface inside its own namespace', () => {
  const block = serviceBlock('lodestar');
  assert.match(block, /LODESTAR_BIND: 0\.0\.0\.0/,
    'the composed board binds loopback inside the container, where nothing can reach it');
  assert.match(block, /LODESTAR_ALLOWED_HOSTS: lodestar:3000/,
    'the brain dials http://lodestar:3000 and the Host allowlist must know that name');
  // Internal service-to-service URLs are unchanged by any of this.
  assert.match(serviceBlock("brain"), /BOARD_API_URL: http:\/\/lodestar:3000/);
  assert.match(block, /AGENT_URL: http:\/\/brain:9000/);
});

// This is a configuration invariant.
test('the composed stack refuses to start without a password verifier', () => {
  const block = serviceBlock('lodestar');
  // `:?` and not `:-`: a default here would be a board on the network with no
  // password, which is the exact state this whole change exists to end.
  assert.match(block, /LODESTAR_AUTH_PASSWORD_HASH: \$\{LODESTAR_AUTH_PASSWORD_HASH:\?/,
    'compose supplies a fallback for the password hash — it must fail instead');
  assert.match(block, /LODESTAR_SERVICE_TOKEN: \$\{LODESTAR_SERVICE_TOKEN:-\}/);
  assert.match(serviceBlock("brain"), /BOARD_API_TOKEN: \$\{LODESTAR_SERVICE_TOKEN:-\}/,
    'the brain and the board must share one service token');
});
