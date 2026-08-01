# Renaming question → card

**Date:** 2026-08-01
**Status:** approved, not yet implemented

## Why

The board began as "question ai engineering" — a place to park questions. It is
now a life dashboard whose cards are questions, problems, tasks, ideas, plans
and habits. A card is the thing on the board; a question is one of six things a
card can *be*. The vocabulary never caught up, so the domain word and the type
name are the same string, and the agent's tool API tells a model to call
`create_question` in order to make a task.

This renames the domain word and leaves the type name alone.

## The rule

One test, applied per occurrence rather than by search-replace:

> **Does this word name a thing on the board?** If yes → `card`. If it names
> something a human or a model *asks* → it stays `question`.

Of roughly 700 occurrences of "question" in the repo, about 120 name a board
card. The rest stay, for four distinct reasons:

1. **The card type.** `TYPES`, `CardType`, the `'question'` column default in
   SQLite, `sample-overview.json`, the type-filter `<option>`, the `?` glyph.
2. **The RAG lab and its ground truth.** `select_questions`, `n_questions`,
   `question_fa`/`question_en`, `score_question`, the 100-question fixture,
   every row already written into `.runs/`. These are questions put to a
   retrieval pipeline. Renaming them would call a Farsi diary query a "card",
   and would make saved runs and the leaderboard unreadable against new code.
3. **`retrieval.py`'s query parameters.** `resolve_time_scope(question)`,
   `keyword_query(question)`, `expand_queries(question)`. The argument is the
   user's query. Renaming it would make `find_related`'s internals claim it
   searches *by* card.
4. **Prose that means a question.** The agent prompt's "break fuzzy questions
   into concrete sub-questions" is about questions.

## What changes

### Brain — the model-facing tool API

| Now | After |
| --- | --- |
| `list_questions` / `ListQuestionsArgs` | `list_cards` / `ListCardsArgs` |
| `create_question` / `CreateQuestionArgs` | `create_card` / `CreateCardArgs` |
| `update_question` / `UpdateQuestionArgs` | `update_card` / `UpdateCardArgs` |

The `@tool('name')` string and the Python function name move together. The
string is what the model sees and the function is what the repo calls; a drift
between them is the exact failure the explicit-args-model convention exists to
prevent.

Three call sites follow, and each is already pinned by a test:

- `server.py`'s `MUTATING_TOOLS` / `PROPOSING_TOOLS` key on the tool *name*. A
  missed rename here does not raise — it silently stops the client adopting
  agent edits and stops the proposals list refreshing.
- `llm.py`'s fake-chat `add:` heuristic emits a `create_question` call.
- `agent.py`'s system prompt: "never invent question ids — look them up with
  `list_questions`" → "card ids … `list_cards`"; "save it into the question's
  notes" → "the card's notes".

`find_related` and `recall_chat` are unaffected.

### Frontend and Node

- `app.js` — `qLabel` → `cardLabel`; `'Q-'` → `'C-'`; the localStorage prefix
  (below); comments where "question" means a card. `ragState.questions` and the
  whole raglab block are untouched.
- `server.js` — comments only. `/api/cards/:id`, `/api/state`,
  `/api/proposals` and the `cards` table already carry the right word, so
  nothing on the wire or in the schema moves.
- `index.html` — the import hint's `Q-012` → `C-012`, and the export button's
  stale `Download questions.json` → `Download lodestar.json` (`app.js:4473`
  has downloaded `lodestar.json` for some time; the label never followed).
  The `<option value="question">? Questions</option>` type filter stays.
- `styles.css` — comments: "stamped question dot" → "stamped card dot", and the
  file header's "the question ledger" → "the card ledger". **No class renames**
  — CSS class names are test-stable API. `.rag-question` is lab-side and stays.

### The card label: `Q-001` → `C-001`

`qLabel` becomes `cardLabel` and the prefix character changes. This is display
only: `card.num` is untouched in SQLite, in `PUT /api/state` and in every
export, and `.card-num` / `.row-num` keep their class names. No card is
renumbered, and the change is reversible by one character.

The cost is accepted deliberately: any reference to a card as "Q-014" in older
notes, commits or conversation no longer matches what the board shows.

### The localStorage migration

Ten keys are prefixed `question-board:` — `v1`, `theme`, `view`, `history`,
`habit-mute`, `proj`, `matrix`, `reviewed`, `resurface`, `models`. Several hold
data that exists nowhere else: the undo timeline, Review/resurface state, the
matrix and projection picks, assistant model choices, habit mute. Renaming the
prefix without a migration silently resets all of it in a browser already in
use.

New prefix is `lodestar:`, matching the existing `lodestar-raglab-config`. One
function at boot, before any key is read:

```js
// Keys were 'question-board:*' before the question→card rename. Copy each across
// once; a browser that already migrated has the new key, so the old one is ignored.
// Delete this once no browser in use predates the rename.
function migrateStorageKeys() {
  try {
    for (const s of LEGACY_SUFFIXES) {
      const old = localStorage.getItem(LEGACY_PREFIX + s);
      if (old !== null && localStorage.getItem(KEY_PREFIX + s) === null) {
        localStorage.setItem(KEY_PREFIX + s, old);
      }
    }
  } catch (_) { /* private mode */ }
}
```

Three properties, and they are what the test asserts:

- **It copies, it does not move.** The old keys stay in place, so an older build
  — or a revert of this commit — still finds the undo timeline and Review state.
  The cost is a few KB of dead storage, which is the cheap side of the trade.
- **`=== null`, not `!old`.** A migrated key the user has since changed must
  never be clobbered by the stale legacy copy on the next boot, and an empty
  string is a real stored value.
- **Idempotent**, so a second call is a no-op.

This ships an obsolescence date. The migration only helps a browser that ran the
old build, it is not self-removing, and nothing in the repo will remind us — so
the removal condition lives in the comment rather than being left implicit.

## Testing

The dangerous coupling is already covered, and the honest thing is to say so
rather than add tests for the look of it:

- `test_tool_schemas.py` pins the six tool names as an exact set (`EXPECTED`).
- `test_server.py` asserts `PROPOSING_TOOLS == {'create_question'}` and
  `MUTATING_TOOLS == {'update_question'}` literally.

A missed rename therefore fails a test instead of silently breaking board
adoption. Existing tests move in the same commit as the code they cover:
`test_board_tools.py`, `test_tool_schemas.py`, `test_llm.py`, `test_server.py`,
`evals/harness.py`, both eval scenario JSONs, and `e2e_test.py`'s tool-chip
check.

New coverage is limited to the two actual behaviour changes, both end-to-end:

- **One migration test** (`e2e_test.py`): seed the ten `question-board:*` keys,
  reload, assert the `lodestar:*` copies exist, the legacy keys survive, and a
  value changed after migration is not clobbered on a second reload. Four
  asserts in one test — edge cases folded in, per the repo's testing policy.
- **One assert** that `.card-num` reads `C-001`, added to the existing
  card-render check rather than standing alone.

## Docs

Split by whether a document describes the present or records the past.

| File | | Why |
| --- | --- | --- |
| `CLAUDE.md` | update | Living instructions; names the tools by their old names |
| `details.md` | update | Full technical tour of the current architecture |
| `plan.md` | update | Current requirements ledger |
| `README.md` | **do not touch** | Carries the user's uncommitted edits. Stale lines get reported, not rewritten |
| `docs/superpowers/specs/*`, `plans/*`, `plan_agenda/*` | leave | Dated records of what was decided then. Rewriting them makes the history lie |

## Landing it

Branch `feat/card-rename`. Four commits, each carrying its own tests and each
passing on its own:

1. `refactor(brain): rename the three board tools from question to card`
2. `refactor(board): rename question to card in the frontend and server`
3. `feat(board): label cards C-001 and migrate the localStorage keys`
4. `docs: adopt card as the board's word`

`npm run test:all` and `uv run --project brain pytest brain/tests -v` before
merge.

## Rejected

- **Blanket search-replace.** Would rename the card type to `card`, making
  `type: 'card'` meaningless, and would rewrite the RAG lab's ground truth.
- **Renaming the RAG lab's `question` identifiers.** They are questions. It
  would also break every saved run in `.runs/` and the leaderboard's ability to
  read old rows.
- **Renaming the localStorage keys with no migration.** Cleanest code, but it
  resets the undo timeline and Review state in the browser actually in use, for
  a string nobody reads.
- **Keeping `Q-001`.** Considered, on the grounds that the prefix is the
  ledger's identity rather than a type name. Rejected in favour of consistency:
  the visible label should match the domain word, and the change costs nothing
  durable.
- **A guard test asserting no board-domain `question` identifier survives.**
  Brittle (it cannot tell the four legitimate families apart) and redundant
  against the two name pins that already exist.
