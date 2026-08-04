"""Every experiment the lab finishes, in one SQLite file.

    databases/test/raglab.db          (RAGLAB_DB overrides)

Three kinds of work run in this lab — build an index, retrieve for a selected
sample, score a sample end to end — and until this module existed only the third
one left anything behind. So "what have I already tried?" was a question the lab
could not answer about two thirds of its own work, and answered about the third
only in the shape the leaderboard needed.

**One row per finished job, written before the job goes terminal.** The ordering
is load-bearing: both frontends and the Inspector poll a job until it stops
running, so a row written *after* `state = 'done'` is a row a follower can look
for and miss.

**The row says where it ran.** `provider` is the resolved backend, not the
payload's request, because `fake` answers and judges without ever failing: a run
on the stub produces a full set of confident numbers that measured nothing, and
the only thing separating it from a real measurement is this column. `sweep.py`
refuses to start on that backend for the same reason; a ledger cannot refuse
anything, so it records instead.

**The detail is the experiment's, not the corpus's.** `detail` holds the whole
result — config, per-question rows, traced candidate ranks, notes, the sample —
which is what makes a row explicable a month later. `chunks_by_session` is
stripped: chunk text is a property of the index fingerprint, byte-identical
across every experiment sharing one, and reproduced exactly by re-running the
build. Keeping it would store the corpus once per experiment.

Why `databases/test/`: `databases/real/` is the only folder `npm run backup`
walks and the only one holding a person's own record. Lab experiments are
reproducible from the fixtures and specific to one machine, so they belong in
the disposable half — and being there means the backup script needs no
exception, which is a rule that cannot be forgotten rather than one that has to
be remembered. `*.db` already keeps it out of git.

**This is not the lab's vector store and must never become one.** The index
still lives in process memory and is discarded with the process, for the reasons
in `store.py`: a sweep rebuilds it dozens of times and the cheapest way for a
stale collection to be found is to still be there. What is durable here is the
record of what ran — never the vectors it ran over.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

from .config import ROOT

# Order matters: it is the column order of the table and of every row this
# module returns, so the panel's table can be rendered from it without a
# hand-kept second list to drift.
COLUMNS = (
    'experiment_id',        # a run's own id, else the job id — never both
    'kind',                 # index | retrieve | run | query
    'state',                # done | error | cancelled
    'label', 'started_at', 'seconds',
    'provider',             # the resolved chat backend: fake means "not a measurement"
    'chunker', 'embedder', 'retriever', 'reranker', 'grader', 'answerer',
    'n_questions',
    'decision', 'decision_stderr',
    'error',
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
  experiment_id   TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  state           TEXT NOT NULL,
  label           TEXT NOT NULL DEFAULT '',
  started_at      TEXT NOT NULL DEFAULT '',
  seconds         REAL NOT NULL DEFAULT 0,
  provider        TEXT NOT NULL DEFAULT '',
  chunker         TEXT NOT NULL DEFAULT '',
  embedder        TEXT NOT NULL DEFAULT '',
  retriever       TEXT NOT NULL DEFAULT '',
  reranker        TEXT NOT NULL DEFAULT '',
  grader          TEXT NOT NULL DEFAULT '',
  answerer        TEXT NOT NULL DEFAULT '',
  n_questions     INTEGER NOT NULL DEFAULT 0,
  -- NULL, never 0.0, on every experiment that judged nothing — which is every
  -- index build, every retrieval, and every run with ragas off. A fabricated
  -- zero would sort below real rows and read as a measured refusal.
  decision        REAL,
  decision_stderr REAL,
  error           TEXT NOT NULL DEFAULT '',
  detail          TEXT NOT NULL DEFAULT '{}'
);
"""

# What never goes into `detail`. One tuple rather than a check per caller: the
# next result shape to carry chunk text should be stripped by having been added
# here, not by whoever writes the route remembering to.
HEAVY = ('chunks_by_session',)


def db_path(env: dict | None = None) -> Path:
    """Where the ledger lives. `RAGLAB_DB` overrides, which is what lets the
    suite point every test at a temp file from one autouse fixture."""
    environ = os.environ if env is None else env
    override = (environ.get('RAGLAB_DB') or '').strip()
    return Path(override) if override else ROOT / 'databases' / 'test' / 'raglab.db'


def connect(path: Path | None = None) -> sqlite3.Connection:
    """An open connection with the schema in place.

    Resolved per call rather than held open for the process: a lab runs for
    hours between experiments, and one row a minute does not pay for a
    connection that can go stale."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    db.execute(SCHEMA)
    return db


def stamp() -> str:
    """Local time, zero-padded, in the format `RunResult.started_at` already
    uses — so one column can hold both without a parse to compare them."""
    return time.strftime('%Y-%m-%d %H:%M:%S')


def row_for(job: dict, state: str) -> dict:
    """One ledger row from one finished job.

    Derived from the job's own config and result rather than passed in by each
    route, so a route added later is recorded by having been run — the same
    reasoning as the untrusted-output middleware. Every field degrades to a
    blank or a zero: an index build has no sample and no score, and a job that
    failed may have no result at all, both of which are facts to record rather
    than reasons not to."""
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    config = job.get('config') or {}
    index = config.get('index') or {}
    # A build's job config carries a full `LabConfig`, so the retrieval and
    # generation groups are populated with defaults — knobs the panel happened to
    # be showing, which no part of a build reads. Recorded, they would put
    # `hybrid-rrf` and `lexical` on a row that never retrieved anything, and a
    # reader comparing rows would attribute a chunk count to a reranker. A build
    # is defined by its index config alone; the rest of the row is honestly blank.
    ran_pipeline = job.get('kind') != 'index'
    retrieval = (config.get('retrieval') or {}) if ran_pipeline else {}
    generation = (config.get('generation') or {}) if ran_pipeline else {}
    summary = result.get('summary') or {}
    ragas = result.get('ragas') or {}
    selection = result.get('selection') or {}
    return {
        # A run is identified by its run id, so this row and the JSON file the
        # leaderboard reads are the same measurement, each checkable against the
        # other. Everything else has only its job id.
        'experiment_id': result.get('run_id') or job.get('id') or '',
        'kind': job.get('kind') or '',
        'state': state,
        'label': result.get('label') or config.get('label') or '',
        'started_at': result.get('started_at') or stamp(),
        # The run's own figure where it has one, so the ledger and the run file
        # never disagree by a rounding; the job's wall clock otherwise, which is
        # the only duration a build or a retrieval has.
        'seconds': round(float(result.get('seconds')
                               or job.get('seconds') or 0.0), 2),
        'provider': config.get('provider') or '',
        'chunker': index.get('chunker') or '',
        # The model, not just the backend: two `fastembed` rows can be two
        # entirely different representations of the same corpus.
        'embedder': index.get('embedder') or '',
        'retriever': retrieval.get('retriever') or '',
        'reranker': retrieval.get('reranker') or '',
        'grader': retrieval.get('grader') or '',
        'answerer': generation.get('answerer') or '',
        'n_questions': int(summary.get('n_questions')
                           or selection.get('n') or 0),
        'decision': ragas.get('decision'),
        'decision_stderr': (ragas.get('decision_spread') or {}).get('stderr'),
        'error': job.get('error') or '',
    }


def detail_for(job: dict) -> dict:
    """The stored payload: the whole result, minus the corpus."""
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    detail = {key: value for key, value in result.items() if key not in HEAVY}
    # The config travels even when the job never produced a result, because a
    # failed experiment is only worth recording if it says what was attempted.
    detail.setdefault('config', job.get('config') or {})
    return detail


def record(job: dict, state: str, path: Path | None = None) -> str:
    """Write one finished job down and return its experiment id.

    Raises on failure, deliberately: the caller decides what a ledger it cannot
    write means, and for a two-hour judged run the answer must never be "lose
    the result". `Jobs.run` catches it and reports it on the job."""
    row = row_for(job, state)
    values = dict(row, detail=json.dumps(detail_for(job), ensure_ascii=False,
                                         default=str))
    fields = tuple(values)
    with connect(path) as db:
        db.execute(
            f'INSERT INTO experiments ({", ".join(fields)}) '
            f'VALUES ({", ".join(":" + name for name in fields)}) '
            # A job id is unique per process and a run id carries a timestamp, so
            # this fires only when the same experiment is recorded twice — a
            # retry, never two different experiments. Last write wins.
            f'ON CONFLICT(experiment_id) DO UPDATE SET '
            + ', '.join(f'{name} = excluded.{name}' for name in fields
                        if name != 'experiment_id'),
            values)
    return row['experiment_id']


def experiments(limit: int = 200, path: Path | None = None) -> list[dict]:
    """Every experiment, newest first, without its detail.

    Ordered by insertion rather than by `started_at`: insertion order is
    completion order, and an evaluation carries the time it *began*, so sorting
    on that column would file a long run behind the short ones that finished
    while it was still going."""
    try:
        with connect(path) as db:
            rows = db.execute(
                f'SELECT {", ".join(COLUMNS)} FROM experiments '
                'ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
    except sqlite3.Error:
        # A ledger that cannot be read is an empty listing, never a 500: the
        # panel's other tables and every run button have nothing to do with it.
        return []
    return [dict(row) for row in rows]


def experiment(experiment_id: str, path: Path | None = None) -> dict | None:
    """One experiment with its detail parsed, or None if the ledger has no such
    row."""
    with connect(path) as db:
        found = db.execute(
            f'SELECT {", ".join(COLUMNS)}, detail FROM experiments '
            'WHERE experiment_id = ?', (experiment_id,)).fetchone()
    if found is None:
        return None
    row = dict(found)
    try:
        row['detail'] = json.loads(row['detail'] or '{}')
    except json.JSONDecodeError:
        # Unreadable detail is reported as such rather than as an empty
        # experiment: the scalar columns beside it are still true.
        row['detail'] = {'unreadable': True}
    return row


"""Alternatives considered
=======================

Why did you write your own persistence layer instead of using an ORM — or just
keeping the JSON files?
--------------------------------------------------------------------------------

Because this is one table with seventeen scalar columns and one JSON blob,
written once per experiment and read by two `SELECT`s. Raw `sqlite3` is 40 lines
of it; every library that would help costs a dependency and a mapping layer to
do the same two statements. The lab is test-only tooling and its whole value is
that a reader can hold it in their head.

**Why the obvious option fails.** The obvious option was to keep doing nothing —
`.runs/*.json` plus a directory listing, which is how the leaderboard already
works. It fails on the question this module exists to answer. `list_runs` globs
a directory and parses every file to build a listing: at 166 files that is 22 MB
of JSON read to render twenty rows, and it can only ever list *evaluations*,
because an index build and a retrieval have no run file. Extending the JSON
scheme to those means inventing a second directory and a second listing parser,
and "which experiments ran on the fake provider?" would still be a full scan of
both. `SELECT ... WHERE provider = 'fake'` is why a database is the right shape
here even though the vector store deliberately is not.

**Why not the framework.** FastAPI and LangChain are already dependencies and
neither offers anything: FastAPI has no storage layer, and LangChain's stores
(`SQLRecordManager`, the chat-message histories) are keyed for indexing and
conversation replay, not for rows the panel ranks. SQLAlchemy is not a
dependency of this project at all, and adding one to the *lab* — which pins
`langchain-openai<1` for ragas and already carries four version pins — buys a
migration story for a table that will not migrate.

**The libraries that would do it.**

- **SQLAlchemy Core** (no ORM) — typed columns, real migrations via Alembic,
  and dialect portability this will never use. The pick on a greenfield service.
- **SQLModel** — Pydantic models as tables, which pairs neatly with FastAPI; it
  would make `row_for` a model constructor. Ties the schema to a Pydantic major
  version, and the lab is already fighting version pins.
- **Dataset** — the smallest of them; `db['experiments'].insert(row)` and
  schema-on-write. Genuinely less code than this file, at the cost of a schema
  nothing states and a table whose columns depend on which row was inserted
  first.
- **Pandas + Parquet** — no schema DDL, and the leaderboard's grouping would be
  a `groupby`. Wrong for the write pattern: one row appended per experiment,
  hours apart, from a background thread.

**Why they were not adopted, and what would change it.** Decisively: the board
server next door is 3,000 lines of backend with **zero npm dependencies** and
`node:sqlite` doing exactly this. A lab that reached for an ORM to write one
table would be the odd one out in its own repo. Behind that, `sqlite3` is
stdlib, so this file adds nothing to `uv.lock` and cannot break the offline
brain suite.

What would change the decision: a second table with a foreign key to this one —
per-question rows promoted out of the `detail` blob so a query could ask "which
questions does every candidate fail?" That is a join, an index, and a migration
of existing rows, and hand-rolled SQL stops being cheaper than SQLAlchemy Core
at roughly the second migration. The measurement that would force it is the blob
itself: if `detail` grows past a few MB per row, storing traces as JSON becomes
the thing to fix, and the fix is a table.
"""
