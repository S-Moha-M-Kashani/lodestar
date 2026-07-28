// tests/helpers/server-harness.mjs
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function waitForLine(proc, regex, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error(`timed out waiting for ${regex}; saw:\n${buf}`));
    }, timeoutMs);
    function onData(chunk) {
      buf += chunk.toString();
      if (regex.test(buf)) { cleanup(); resolve(); }
    }
    function onExit(code) {
      cleanup();
      reject(new Error(`server exited early (code ${code}); saw:\n${buf}`));
    }
    function cleanup() {
      clearTimeout(timer);
      proc.stdout.off('data', onData);
      proc.off('exit', onExit);
    }
    proc.stdout.on('data', onData);
    proc.on('exit', onExit);
  });
}

export async function startServer({ env = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'lodestar-srv-'));
  const dbPath = join(dir, 'board.db');
  // Pick a port unlikely to collide; retry once on early exit.
  const port = 20000 + Math.floor((Date.now() % 40000));
  const proc = spawn('node', ['server.js'], {
    cwd: ROOT,
    // Write-triggered backups are OFF by default here: most tests create cards,
    // and each one would otherwise drop a snapshot of a temp board into the
    // user's real backups/ and evict a genuine one. The backup tests opt in
    // explicitly via `env`, pointing at a temp directory.
    env: { ...process.env, PORT: String(port), BOARD_DB: dbPath, NODE_NO_WARNINGS: '1',
           LODESTAR_BACKUP_ON_WRITE: '0', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {}); // drain
  await waitForLine(proc, new RegExp(`Lodestar running at http://localhost:${port}\\b`));
  const base = `http://127.0.0.1:${port}`;
  const stop = async () => {
    proc.kill('SIGKILL');
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
  };
  return { port, dbPath, dir, base, proc, stop };
}

export { waitForLine };
