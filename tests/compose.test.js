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
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

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

test('the database still lives on its own named volume, not the source tree', () => {
  // The durability promise: adding a source mount must not displace board-data,
  // and BOARD_DB must stay on it — a tree mount is read-only, so a board.db
  // pointed inside it would fail to open at all.
  const block = serviceBlock('lodestar');
  assert.ok(
    mountsOf(block).includes('board-data:/data'),
    'the board-data volume mount is gone — board.db would live inside the ' +
      'container and vanish on the next rebuild',
  );
  assert.match(
    block,
    /BOARD_DB:\s*\/data\/board\.db/,
    'BOARD_DB no longer points into the mounted volume',
  );
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

test('the brain source mount is read-only', () => {
  const mount = brainSourceMount();
  assert.ok(mount, 'the brain source is not mounted at all');
  assert.ok(
    mount.endsWith(':ro'),
    `the brain source is mounted writable ("${mount}") — the container could ` +
      `overwrite the files it imports from`,
  );
});

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

test('the source mount targets exactly where the Dockerfile puts the source', () => {
  // A WORKDIR change would otherwise leave the tree mounted in a directory the
  // server does not run from, and :3000 would go back to serving stale assets
  // with nothing failing.
  const dir = workdir();
  const mount = sourceMount();
  assert.ok(mount, `nothing is mounted at the Dockerfile WORKDIR ${dir}`);
  assert.equal(mount.split(':')[1], dir);
});
