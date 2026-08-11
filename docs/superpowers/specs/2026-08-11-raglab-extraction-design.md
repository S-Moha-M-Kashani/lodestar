# Moving the RAG lab out of Lodestar

The RAG lab is a retrieval workbench that has lived at `brain/tests/raglab/`
since it was built. It has outgrown the directory: 25 modules and ~7,900 lines of
Python, its own two-page frontend, a 4,809-line test file, 737 KB of fixtures, a
6.6 MB experiment ledger, and a second frontend built into the board itself. None
of it serves a person using Lodestar.

This spec moves the whole thing to its own project and removes every trace from
Lodestar. The goal is stated plainly so later decisions can be checked against
it: **Lodestar gets smaller.** The lab is relocated, not redesigned — no new
abstractions, no corpus plugin seam, no renames inside the package.

## Decisions taken

| Question | Decision |
| --- | --- |
| Why move it | Slim Lodestar down; relocate as-is |
| The board's `raglab` view, styles, proxy | Deleted outright |
| The three `lodestar_brain` imports | Vendor a copy; freeze the baseline |
| The graded submission that cites lab paths | Settled — cut everywhere |
| Tooling | Pure Python, uv only; no `package.json` |
| Location and history | `~/Projects/raglab`, fresh `git init` |

The submission decision is the one that unlocks the rest.
`docs/report/project_requirements_checklist.md` cites `brain/tests/raglab/…`
about twelve times as evidence for graded requirements, and
`docs/report/index.html` §05 is an entire section on the lab with seven
screenshots. Those paths are on `master`, the branch published to
`TuringCollegeSubmissions`. Grading is settled, so the citations become a
historical record rather than a live evidence trail.

## The new project

`~/Projects/raglab` — uv-managed Python 3.13, package `raglab`, one initial
commit. It builds an index, retrieves, evaluates, records every experiment in a
ledger, serves the panel on `:9002` and the read-only Inspector on `:9003`.

The corpus stays the Persian diary year and the configuration it measures against
stays Lodestar's, frozen and dated. The lab remains *about* Lodestar's retrieval;
it simply no longer lives inside it.

```
raglab/
  pyproject.toml  uv.lock  README.md  CLAUDE.md  .env.example  .gitignore
  src/raglab/
    __init__.py chunking.py config.py corpus.py embedding.py evaluate.py
    explain.py export.py index.py inspector.py judgescreen.py leaderboard.py
    ledger.py llm.py metrics.py models.py pipeline.py present.py query.py
    ragas_eval.py retrieval.py server.py store.py sweep.py
    baseline.py          # new — the frozen Lodestar snapshot
    textnorm.py          # vendored from lodestar_brain
    cli/lab.py           # scripts/lab.mjs, reimplemented
    static/  index.html inspector.html inspector.css inspector.js sorttable.js
  fixtures/  diary_year_fa.json  diary_year_fa_groundtruth.json
  tests/
    conftest.py test_raglab.py test_inspector.py test_config.py
    test_ports.py test_sorttable.py test_no_lodestar.py
  docs/
    rag-architecture.md  rag-sweep-2026-07-30.json  rag-test/
    plans/2026-08-03-raglab-inspector.md
  databases/raglab.db    # the ledger, gitignored as today
  .runs/  .screens/
```

`brain/tests/raglab/CLAUDE.md` becomes the new repo's `CLAUDE.md` — it is already
written as the lab's own guide, so it moves rather than being rewritten. The
Inspector plan is still ACTIVE and follows the code it plans.

## Cutting the three cords

The lab imports `lodestar_brain` in ten places. Each is cut differently, because
each carries a different risk.

### `textnorm` — vendored

Eight modules share Lodestar's tokeniser. The 151-line `textnorm.py` is copied
into `src/raglab/` with its `Alternatives considered` docstring intact, plus one
provenance line naming the source commit and the date. `from lodestar_brain
import textnorm` becomes `from . import textnorm`.

The accepted cost: two tokenisers that can drift, and if Lodestar's ever changes,
the lab is quietly measuring a different one. The provenance line is what makes
that discoverable instead of invisible. It is not a guarantee.

### `make_chat_model` and `Settings` — a local factory

`raglab/llm.py` currently translates `LabSettings` into the brain's `Settings` so
it can call the production seam. That indirection disappears: the three branches
of `make_chat_model` (`openrouter`, `ollama`, `fake`) and `FakeChat` come across
as `raglab.llm.make_chat_model(LabSettings)`, selected by `RAGLAB_LLM`. An
unknown value raises — the house rule that a backend is named and never inferred
survives the move.

`brain_settings()` is deleted. `lab_llm`, `judge_llm` and `lab_chat` keep their
signatures and their docstrings, since what they document (why the judge gets its
own client, why an empty model is a branch rather than a default argument) is
still true.

### The shipped baseline — frozen

`config.py:429–456` imports `lodestar_brain.config` and `lodestar_brain.retrieval`
to derive the "production" preset live. It becomes `baseline.py`: a dict of
literals, each with a comment naming the constant it was read from, headed by the
snapshot date and the Lodestar commit.

The panel's *"use the project's own RAG settings"* button relabels to name the
snapshot. A preset that silently stops being true is worse than one that admits
its age, and the button is where a person reads it.

### The guardrail

`tests/test_no_lodestar.py` asserts that no file in the repo imports
`lodestar_brain`. The invariant is an absence, pinned the way
`test_guardrails.py` pins the brain having no `save_cards`: a future edit cannot
reintroduce the dependency by habit.

## Tooling: the pin incantation dissolves

`npm run raglab` carries six flags today:

```
--extra semantic --extra local-embeddings --with 'ragas==0.4.*'
--with 'langchain-community<0.4' --with 'langchain-openai<1' --with rapidfuzz
```

The four `--with` pins exist **only because ragas 0.4 drags `langchain-core` down
to 0.3.x, which would break Lodestar's langchain-1.x brain.** With no brain in the
repository they become ordinary locked dependencies. One incantation, currently
duplicated across `package.json`, `scripts/lab.mjs` and two `ports.test.js`
assertions, collapses into `uv.lock`. This is the move's one genuine improvement,
and it is a consequence of the separation rather than a change of design.

Dependencies, derived from the lab's own imports:

| Where | Packages |
| --- | --- |
| `dependencies` | `fastapi`, `uvicorn`, `numpy`, `httpx`, `langchain-core`, `langchain-openai<1`, `langchain-community<0.4`, `ragas==0.4.*`, `rapidfuzz` |
| `[project.optional-dependencies] semantic` | `fastembed>=0.7` |
| `[project.optional-dependencies] local-embeddings` | `sentence-transformers>=3.0` |
| `[dependency-groups] dev` | `pytest>=8.0` |

Chroma and `rank-bm25` are deliberately absent: the lab's index is process memory
and its BM25 is its own, which is why `retrieval.py` exists.

`langchain-core` is left unpinned and will resolve to 0.3.x, because ragas 0.4
requires it. That is the same environment the lab runs in today, and everything it
uses — `BaseChatModel` and `.invoke()` — exists in both majors. The constraint that
mattered was never the lab's; it was that this resolution must not touch the
brain, and after the move there is no brain to touch. `llm.py`'s docstring says so
and stays accurate.

Entry points, replacing the npm scripts:

| Command | Was |
| --- | --- |
| `raglab` | `npm run raglab` — serve `:9002` |
| `raglab-inspector` | `npm run raglab:inspector` — serve `:9003` |
| `raglab-sweep` | `npm run raglab:sweep` |
| `raglab-judgescreen` | `npm run raglab:judgescreen` |
| `raglab-leaderboard` | `npm run raglab:leaderboard` |
| `raglab-lab` | `npm run lab` — suite first, refuses to serve on red |

`cli/lab.py` reproduces `lab.mjs` exactly: run the suite, refuse to open the
panel on a red one, `--no-test` / `--all` / `--test-only`. Its reason for
existing is unchanged — the lab's whole claim is that retrieval was decided by
measurement, and that claim is worth what the suite behind it is worth.

torch stays an extra rather than a default dependency. The default embedder is
`sentence-transformers`, so `uv run --extra local-embeddings raglab` is the
documented launch and the test suite keeps running torch-free. Without the extra
the service starts and then fails on the first index build, which reads as "the
lab is broken" rather than "install this" — so `test_ports.py` inherits today's
invariant, now checking the README's documented command against the config's
default embedder rather than a `package.json` script.

## Ports, and one invariant that gets weaker

`:9002` and `:9003` stay. But the new repo cannot read Lodestar's
`package.json`, so "these collide with no board, brain, or Chroma" becomes a
hardcoded reserved set in `test_ports.py`, commented with Lodestar's allocation
(3000/3001 boards, 9000/9001 brains, 8003/8004 Chroma, 8001/8002 the external
vectordb-lab stack).

This is genuinely weaker than what `tests/ports.test.js` does today: two repos
can now drift into a collision that no test catches. It is recorded here as a
known loss rather than presented as an equivalent move.

## Tests in the new repo

Everything that tests the lab moves with it. Three files are new or reshaped:

- **`conftest.py`** moves unchanged. All three of its session guards are
  lab-specific — pinning `RAGLAB_LLM=fake` so the suite measures the code and not
  whether Ollama happens to be running, and redirecting `RAGLAB_DB` and
  `RUNS_DIR` away from the durable ledger. Lodestar's `brain/tests/conftest.py`
  therefore disappears entirely rather than being trimmed.
- **`test_ports.py`** carries over the surviving invariants from
  `tests/ports.test.js`: the two ports, the reserved set, the launcher/extra
  pairing, that no lab command names a vector database, and that the one-command
  runner delegates its launch rather than respelling it.
- **`test_sorttable.py`** shells out to `node --test` and skips with a clear
  reason when node is absent — the same honest skip as
  `test_chat_memory_server.py` when `:8004` is down. `sorttable.js` is a browser
  file whose ordering logic (`cellKey`, `compare`, missing-values-last) is real
  and tested; dropping the test to satisfy "pure Python" would be losing
  something for a slogan.
- **`test_config.py`** is a new file adapted from Lodestar's env audit rather than
  a move: Lodestar keeps its own `brain/tests/test_config.py` (it tests more than
  the lab), and only the `raglab` entry in its source list goes. The new repo gets
  the same both-directions assertion over its own sources and `.env.example`.

### Eight tests lose half their subject

`test_raglab.py` reads Lodestar's `app.js` in eight tests, because until now
there were two frontends over one API and the invariant worth pinning was that
they could not disagree. Deleting the board's view deletes half of that subject,
so "relocate as-is" is impossible here and these are the one set of tests the
move genuinely rewrites:

| Test | Becomes |
| --- | --- |
| `test_both_frontends_read_the_progress_detail` | the panel alone |
| `test_both_rag_lab_frontends_offer_a_cooperative_stop` | the panel alone |
| `test_both_frontends_watch_the_ask_as_a_job` | the panel alone |
| `test_both_panels_send_you_to_the_inspector` | the panel alone |
| `test_neither_panel_still_asks_one_question` | the panel alone |
| `test_both_panels_offer_the_mode_dropdown` | the panel alone |
| `test_both_panels_fill_the_projects_settings_from_the_served_preset` | the panel alone; the "no frontend keeps its own copy of the preset" half survives for the one frontend left |
| `test_every_board_lab_control_has_a_stable_id` | **deleted** — its whole subject is the board's `raglab-<field>` ids |

Each rewrite keeps the assertion and narrows the subject. None of them loosens
into a weaker claim about the same thing, which is the failure mode worth naming:
a test that used to compare two implementations and now merely checks that one of
them mentions a string is not the same test with one argument removed.

## What leaves Lodestar

| File | Change |
| --- | --- |
| `brain/tests/raglab/` | deleted (whole tree) |
| `brain/tests/fixtures/` | moved (diary + ground truth; nothing else reads them) |
| `brain/tests/conftest.py` | deleted — all three guards are the lab's |
| `brain/tests/test_raglab.py`, `test_inspector.py` | moved |
| `brain/tests/test_config.py` | drop `raglab` from the env-audit source list |
| `brain/src/lodestar_brain/llm.py` | the comment at line 10 naming `npm run raglab` |
| `app.js` | the `raglab` view, its `VIEWS`/`VIEW_LABELS` entries, `#raglab-open`, the ~600-line panel |
| `styles.css` | the `.raglab*` rules |
| `server.js` | `RAGLAB_URL` and the `/api/raglab/` proxy branch |
| `package.json` | `raglab`, `raglab:inspector`, `raglab:sweep`, `raglab:judgescreen`, `raglab:leaderboard`, `lab`, `test:raglab` |
| `scripts/lab.mjs` | deleted |
| `tests/sorttable.test.js` | moved |
| `tests/ports.test.js` | the eleven lab tests; the port-distinctness test loses 9002/9003 |
| `tests/server.test.js` | the four `/api/raglab/*` proxy tests |
| `tests/e2e_test.py` | ~150 checks (roughly lines 2927–3940) and the `RAGLAB_PORT` pin |
| `tests/databases.test.js` | the ledger-path assertion |
| `.env.example` | the `RAGLAB_*` block |
| `CLAUDE.md`, `brain/CLAUDE.md`, `docs/README.md` | the lab's sections become one pointer line |
| `docs/report/rag-architecture.md`, `rag-sweep-2026-07-30.json`, `docs/rag-test/` | moved |
| `docs/report/index.html` §05, `project_requirements_checklist.md` | text and screenshots kept; one line added saying where the lab went |
| `databases/test/raglab.db`, `.runs/`, `.screens/` | moved (untracked data) |

`docs/report/rag-architecture.md` opens by linking
`rag-chosen-architecture.md`, which does not exist anywhere in the repository —
a dangling reference that predates this move. Since the file is being relocated
anyway, the link is resolved during the move: either the missing document is
folded in as a section or the sentence is dropped. A moved document should not
carry a broken promise into a new repo.

The seven `raglab-*.png` screenshots in the report are produced by the e2e checks
being deleted. They already exist as files, so the report keeps rendering — but it
can no longer be regenerated from Lodestar. With grading settled that is
acceptable, and it is the reason the report keeps its text rather than being
rewritten.

## Order of work

Copy first, prove it green, delete second. Lodestar's side happens on
`chore/extract-raglab`; the new repo gets its initial commit only once its suite
passes.

1. **Stand up `~/Projects/raglab`** — layout, `pyproject.toml`, the three cords
   cut, everything moved. `uv run pytest` green. `test_raglab.py` is 4,809 lines
   and is the real gate.
2. **Open both panels side by side** against the current board build, confirming
   the standalone page has not drifted behind the board's view before that view
   dies. If it has, the difference is either ported or explicitly abandoned —
   not discovered after the delete.
3. **Delete from Lodestar**, verified by `node --test tests/*.test.js`,
   `uv run --project brain pytest brain/tests`, and the full e2e.

`test_config.py`'s `.env.example` audit is the built-in tripwire for step 3: it
asserts both directions, so code removed with variables left documented — or the
reverse — fails rather than passing quietly.

## Out of scope, and accepted losses

- **No backup story for the new repo.** The 6.6 MB ledger is gitignored, as it is
  today, and `npm run backup` never covered it. It is one machine copy away from
  gone. Worth a follow-up; not part of this move.
- **The port-collision invariant weakens**, as recorded above.
- **The vendored tokeniser can drift**, as recorded above.
- **One origin is lost.** The lab is browsed at `localhost:9002` directly. It
  serves its own `sorttable.js`, so nothing breaks.
- **No generalisation.** No corpus plugin seam, no baseline seam with two modes,
  no renames inside the package. Those are a different project, and the goal here
  is a smaller Lodestar.
