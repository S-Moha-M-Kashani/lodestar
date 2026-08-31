// db/backend.mjs — which store server.js opens.
//
// The project's seam convention (CLAUDE.md invariant 3), applied to storage:
// a backend is NAMED, there is no `auto` mode, and an unknown value raises at
// boot rather than falling back. Extend by adding to BACKENDS and branching at
// the call site, never by making this function guess.
//
// The default is sqlite and stays sqlite until the Postgres path has carried
// the real board. Flipping it is its own commit, with its own decision.

export const BACKENDS = ['sqlite', 'postgres'];

/** Decide the backend from the environment. Throws on anything unrecognised. */
export function chooseBackend(env = process.env) {
  const chosen = (env.LODESTAR_DB_BACKEND || '').trim() || 'sqlite';
  if (!BACKENDS.includes(chosen)) {
    throw new Error(
      `LODESTAR_DB_BACKEND is "${chosen}", which is not a backend. ` +
      `Use one of: ${BACKENDS.join(', ')}. There is deliberately no auto mode.`);
  }
  if (chosen === 'postgres' && !(env.LODESTAR_PG_URL || '').trim()) {
    throw new Error(
      'LODESTAR_DB_BACKEND=postgres needs LODESTAR_PG_URL. Load it with: ' +
      'set -a; . ~/Projects/postgres/.lodestar-url; set +a');
  }
  return chosen;
}
