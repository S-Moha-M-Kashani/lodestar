# Who owns the board — sync, staleness, and the merge (2026-08-22)

## The incident

Two people, two laptops, one board server. On 2026-08-22 the second machine
opened the board and, within six minutes, 24 cards on `main` — `C-042`…`C-067` —
were soft-deleted. Nobody deleted anything.

The evidence, from this machine's own backups:
`backups/db/board-2026-08-22T13-35-15-071Z.db` holds 40 live cards on `main`;
the snapshot 22 minutes earlier holds 64. No card carries a `deleted_at` in that
window *today*, because a browser on the first laptop later pushed the whole
board again and the upsert's `deleted_at = NULL` un-archived all 24. The loss was
real, silent, and undone by luck.

Two rules, each defensible alone, combined into it:

1. `js/core/sync.js` — *"a browser that already has its own board wins on
   load"*. If `localStorage` held a board, the browser pushed it and never read
   what the database held. Written for one browser, where it is exactly right:
   unsynced local edits can never be clobbered by the server.
2. `server.js` `writeBoard()` — the whole-board `PUT /api/state` soft-deletes
   every live card absent from the payload. That is what makes deletion work at
   all when the only write is a whole document.

A `localStorage` copy from any earlier day was therefore a licence to delete
everything newer, and there was no revision, ETag or concurrency check anywhere
in the stack: last PUT won.

## The rule that replaces it

**The server owns the board whenever it answers. This browser's copy is pushed
as the truth in exactly one case: when it holds changes the server never
acknowledged.**

`localStorage` keeps its other jobs — theme, view, undo timeline, filters, model
picks, the chat crash-net — and keeps caching the board so the app still works
with no backend. What it loses is *authority* over cards and categories, the
only two things the database also holds and the only two this bug could destroy.

Removing browser storage altogether was considered and rejected: 47 call sites
across 17 modules, and almost all of them hold state with no server home. The
fix is about who wins, not about where preferences live.

## Two layers, because they fail differently

### The browser: adopt, or merge — never overwrite

`initServerSync` has three outcomes, decided by whether this browser holds
anything the server has not acknowledged:

| This browser | On load |
| --- | --- |
| no saved board (first ever open) | adopt the server's board |
| saved board, watermark matches it | adopt the server's board |
| saved board, watermark missing or stale | **merge**, then push the merge |

The watermark (`lodestar:synced[:board]`, `{ fp }`) is a hash of
`boardFingerprint(cards)` written at the last *acknowledged* sync — a 2xx save,
or a board adopted from the server — and always from the cards that actually
travelled, never from `state.cards` as it stands when the reply lands.

"Unsynced" is therefore **observed, not promised**. A boolean set in a failure
path cannot see a tab closed while a save was in flight: there is no failure to
catch, and the next load would call itself clean. A comparison notices.

`loadedFromStorage` is the first term of that test and is load-bearing: a
browser opening for the first time holds the **seed** cards, which are an
explanation of the app rather than anyone's work. Merging those into an existing
board writes a second copy of all six onto the server — which is not a
hypothetical, it is what the e2e suite caught when this was built without that
term.

The merge itself is `js/core/merge.js`, which imports nothing (the `asana.js`
rule) so it is unit-testable under plain node: local order leads, server-only
cards are appended, and where both sides know a card the higher `updatedAt`
wins. A local-only card is kept **unless the server has it in the Trash** —
`GET /api/trash` supplies those tombstones, and without them two machines could
never delete anything, because the stale one would re-add every deletion.

The registry merges the same way and for a sharper reason: the push that follows
carries a fresh rev, so the server will *replace* the registry with whatever it
names. Sending only the local copy would wipe a life area added on the other
machine — and `cleanCard` would then blank that category on every card holding
it, in the one table with no Trash.

### The server: a save that is behind may add, never delete

The browser fix does not cover a *live* tab left open on the other laptop for an
hour: no `localStorage` staleness is involved, and it will happily save a board
it stopped being right about.

So `PUT /api/state` now carries the version of the board the save was written
against. Three states, one field, and the difference between them is only ever
whether **deleting** is authorised:

- **absent** — the old contract, sweep included. Every `curl`, every eval, the
  brain, and every pre-existing test lives here.
- **equal to the current rev** — this client is describing what the database
  holds, so an omitted card really was deleted.
- **anything else, `''` included** — the client is describing a board that has
  moved. The write is applied *additively*: `mergeBoard` inserts what the board
  lacks and updates only what the client is not behind on; `mergeCategories`
  inserts missing ids and touches nothing else. The response says
  `stale: true` and carries the merged board, so the client adopts it and its
  next save is authorised again.

Clients always send the field, `''` when they have no rev yet, so no client path
can be granted the right to delete by forgetting to say anything.

`rev` is `sha1(JSON.stringify(readBoard(id)))`, truncated — a hash of **the
exact bytes the client was sent**, so it has no blind spot by construction.

Rejected alternatives, both tempting and both wrong here:

- A **SQL aggregate** (row count + trashed count + `MAX(updated_at)` + category
  count). Cheap, and blind to two things that are this feature's whole reason for
  existing: `updated_at` comes from the client's own clock, so an edit made by a
  laptop running a minute behind changes no term; and a category *rename*
  changes no count at all.
- A **monotonic `rev` column** on `boards`. Exact, but it must be bumped by
  every path that touches a card — whole-board save, proposal confirm, purge,
  restore — and the day one of them forgets, deletion stops working with nothing
  to notice it. A hash of what was sent cannot be forgotten.

The read-compare-write needs no lock: `node:sqlite` is synchronous and the
handler's only `await` (`readBody`) is already done. Nothing may put an `await`
between the comparison and the write.

## What this costs, deliberately

- **A card deleted while offline can come back.** The merge keeps local-only
  cards, and a deletion that never reached the server leaves no tombstone. This
  board's promise is that a thought is never lost; resurrection is the direction
  it errs in.
- **A *purged* card can come back** for the same reason — it is in neither list.
  Purge is rare and deliberate.
- **A reorder can be reverted.** `position` is re-derived from array order on
  every save and never bumps `updatedAt`, so a merge cannot tell a reordered
  board from an untouched one. Order is cosmetic; a card is not. This is also
  why the "only if newer" comparison happens in JavaScript rather than as
  `ON CONFLICT DO UPDATE … WHERE excluded.updated_at >= cards.updated_at`: that
  one statement also writes `position`, so the guard would drop legitimate
  reorders along with stale content.
- **After any out-of-band write, this browser's next save is merged rather than
  obeyed**, and it is told so ("The board had newer changes — they have been
  merged in"). A delete made in that moment has to be made again. That is the
  protection working, and the e2e suite asserts it from the other side.
- **Not a CRDT.** Per-field vector clocks would remove the last-write-wins guess
  entirely and are the honest answer if this board ever gets simultaneous
  editors. For two laptops that take turns it is a large amount of machinery to
  decide what a timestamp already decides, and it would have to survive
  `PUT /api/state` being a whole document rather than a stream of operations.

## Two bugs fixed on the way

- **Overlapping saves.** `pushToServer` debounced but did not serialise, so two
  whole-board PUTs could be in flight at once — and the first one's payload
  still contains the card the second one deleted, so whichever landed last
  decided. Now one save is in flight at a time and later changes coalesce.
- **A save dropped by a board switch.** `leaveFor` cancelled the pending push,
  so an edit made in the 150 ms before switching boards was lost — and the
  browser then correctly looked like it held unsynced work. It now flushes,
  while the *delete* path still cancels: there the board is about to be gone.

## Tests

- `tests/merge.test.js` — unit, the merge rule and the tombstone exception.
- `tests/boards.test.js` — integration, *a save that names a rev it has not seen
  adds and never deletes*: a matching rev still sweeps, a stale one adds without
  archiving, refuses to revert a newer card, adds a missing category and keeps
  the ones it never saw, and a save with no rev behaves exactly as before.
- `tests/e2e_test.py` — end-to-end, the incident itself: a second browser
  context seeded with a stale `lodestar:v1` (plus one card only it has) loads
  the board; nothing is archived, its own unsynced card is pushed, and it shows
  the merged board.
