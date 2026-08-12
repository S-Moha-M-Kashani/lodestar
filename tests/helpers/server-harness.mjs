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
      const match = buf.match(regex);
      if (match) { cleanup(); resolve(match); }
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
  // Both databases point into the temp dir: without ASSISTANT_DB a spawned
  // test server would write chat rows into the repo's real databases/.
  const assistantDbPath = join(dir, 'assistant.db');
  // PORT=0 asks the kernel for a free port and the server reports the one it
  // got. Every test file used to derive a port from the clock instead, so two
  // suites starting in the same millisecond-ish window picked the same number
  // and one of them died at bind — a flake that moved to a different test on
  // every run and had nothing to do with the test it failed.
  const proc = spawn('node', ['server.js'], {
    cwd: ROOT,
    // Write-triggered backups are OFF by default here: most tests create cards,
    // and each one would otherwise drop a snapshot of a temp board into the
    // user's real backups/ and evict a genuine one. The backup tests opt in
    // explicitly via `env`, pointing at a temp directory.
    env: { ...process.env, PORT: '0', BOARD_DB: dbPath,
           ASSISTANT_DB: assistantDbPath, NODE_NO_WARNINGS: '1',
           LODESTAR_BACKUP_ON_WRITE: '0', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  proc.stderr.on('data', () => {}); // drain
  const [, bound] = await waitForLine(proc, /Lodestar running at http:\/\/localhost:(\d+)\b/);
  const port = Number(bound);
  const base = `http://127.0.0.1:${port}`;
  const stop = async () => {
    proc.kill('SIGKILL');
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
  };
  return { port, dbPath, assistantDbPath, dir, base, proc, stop };
}

export { waitForLine };
