// mcp/board.mjs — the board's HTTP API, as one client.
//
// This module is the whole of the MCP server's reach into Lodestar, and it is
// deliberately narrow in the same way `BoardClient` is on the brain's side:
// there is exactly one write, it is the whole board, and there is no call that
// can erase a card. `DELETE /api/cards/:id` — the board's only hard delete —
// is absent rather than gated, because a purge reachable from a tool is a
// purge one mistaken argument away, and the owner asked for it to stay on the
// board itself.
//
// **Over HTTP and never over the database file.** `server.js` owns board.db:
// it holds the rev handshake, the soft-delete sweep, the ledger, the write-
// triggered backup and the lineage resolution. A second process opening the
// same file would have to reimplement all five and would still race the first.
// So the running board is the single writer, exactly as it is for the brain.
//
// **Invariant 1, in one place.** `PUT /api/state` soft-deletes every live card
// the save omits. Every write here is therefore read-full-state → modify →
// PUT-everything, which is what `mutate` below is; no caller assembles a card
// list of its own.
import { readFileSync } from 'node:fs';

/** Where the board is answering, and the credential to reach it with.
 *
 *  The service token and not the password: this is a non-browser local client,
 *  which is the case that credential exists for, and it means no plaintext
 *  password is ever held by a second process. `server.js` treats it as
 *  `'service'` — the same actor the brain is. */
function config(env = process.env) {
  const base = (env.LODESTAR_BOARD_URL || 'http://127.0.0.1:3000').replace(/\/+$/, '');
  const token = (env.LODESTAR_SERVICE_TOKEN || '').trim();
  return { base, token };
}

/** `.env`, read the board's own way — only `LODESTAR_*` travels and a value
 *  already in the environment wins. Without it the token reaches this process
 *  only if the operator exported it by hand, which is the failure
 *  `scripts/env-file.mjs` was written to end; an MCP client launched by a
 *  desktop app has no shell to export anything in at all. */
export async function loadEnv(root) {
  try {
    const { applyEnvFile } = await import('../scripts/env-file.mjs');
    applyEnvFile({ root });
  } catch { /* no .env, or no reader: the environment is then the whole story */ }
}

class BoardError extends Error {}

/** One request, with the credential attached and a readable failure.
 *
 *  A refusal is turned into a sentence naming what to do about it, because the
 *  reader of this error is a person in a terminal being told why their board
 *  did not answer — "fetch failed" over a stopped server is the message that
 *  sends somebody to debug the wrong service. */
async function ask(path, init = {}, env = process.env) {
  const { base, token } = config(env);
  let res;
  try {
    res = await fetch(base + path, {
      ...init,
      headers: {
        'content-type': 'application/json',
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        ...(init.headers || {}),
      },
    });
  } catch (err) {
    throw new BoardError(
      `The board at ${base} did not answer (${err.message}). Start it with `
      + '`npm start`, or set LODESTAR_BOARD_URL to where it is listening.');
  }
  if (res.status === 401 || res.status === 403) {
    throw new BoardError(
      `The board at ${base} refused this credential (${res.status}). Set `
      + 'LODESTAR_SERVICE_TOKEN to the same value the board was started with.');
  }
  const body = await res.text();
  let parsed;
  try { parsed = body ? JSON.parse(body) : {}; } catch { parsed = {}; }
  if (!res.ok) {
    throw new BoardError(parsed.error || `The board answered ${res.status}.`);
  }
  return parsed;
}

const query = (board) => (board ? `?board=${encodeURIComponent(board)}` : '');

/** The whole board: its live cards, its life areas, and the rev naming the
 *  exact bytes this read was answered with. */
export const getState = (board, env) => ask(`/api/state${query(board)}`, {}, env);

/** The Trash — soft-deleted cards, newest deletion first. */
export const getTrash = (board, env) => ask(`/api/trash${query(board)}`, {}, env);

/** The boards themselves, and which one an unnamed request is answered with. */
export const getBoards = (env) => ask('/api/boards', {}, env);

/**
 * Read the whole board, let `change` rewrite the card list, and PUT all of it.
 *
 * `rev` is the version this save was written against, and it decides one thing
 * only: whether omitting a card is authorised to delete it. Sending it means a
 * delete really deletes; the board answering `stale: true` means somebody else
 * wrote in between and the save was applied additively — so the omission did
 * nothing, and the honest answer is to read again and retry rather than to
 * report a delete that did not happen.
 *
 * One retry and no more. Two clients writing this board in the same second is
 * a real thing (a browser is usually open); a loop that kept trying would be a
 * tool that hangs instead of one that says what happened.
 *
 * **`change` must be idempotent, and that is not a style note.** A stale save
 * is applied *additively* rather than refused, so by the time the retry runs,
 * an added card is already on the board — and a `change` that simply appends
 * would append a second one. Every caller therefore writes its change as
 * "the board, with this card in it" rather than "the board, plus this card":
 * the id is minted before the loop and the card is filtered out before it is
 * put back, so running the function twice produces the same board once.
 */
export async function mutate(board, change, env = process.env) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const state = await getState(board, env);
    const cards = change(state);
    // `categories` is deliberately not sent: the registry is the browser's to
    // write, and a client that echoed back a copy it did not edit is exactly
    // how a life area gets wiped off every card holding it.
    const saved = await ask(`/api/state${query(board)}`, {
      method: 'PUT',
      body: JSON.stringify({ version: state.version ?? 1, cards, rev: state.rev }),
    }, env);
    if (!saved.stale) return saved;
  }
  throw new BoardError(
    'The board changed while this was being written, twice over — something '
    + 'else is saving to it right now. Nothing was lost; try again.');
}
