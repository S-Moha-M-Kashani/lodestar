"""Build the leaderboard from `.runs/`, and refuse to rank rows that are not
comparable.

    npm run raglab:leaderboard              # print it
    npm run raglab:leaderboard -- --write docs/rag-leaderboard.md

The point of this module is the refusal. A decision score is a mean over
questions, judged by a model — so it is comparable only against rows that scored
**the same questions with the same judge**. One flat ranking over everything on
disk is precisely where that gets forgotten, and this lab has already produced
both kinds of incomparable pair: the sample moved from 24 strided questions to 30
balanced ones, and the judge moved from `openai/gpt-5-mini` to a model on this
machine. Either change alone makes a row a different measurement; presented in one
sorted list they read as a ranking.

So: **group first, rank second.** A group is one (question set, judge) pair. Rows
are ranked inside a group and never across groups, and each group states in its
own heading which sample and which judge it is.

The second refusal is about margins. `verdict()` calls a group a tie when the lead
is inside the error, because 0.6487 against 0.6501 was a real pair here — opposite
changes to one knob, precision and recall merely trading places — and a bare
ranking read it as a win. A group whose rows carry no measured error returns
'unknown' rather than a winner: the runs predating `decision_spread` cannot have
one reconstructed, and `± 0` would present the oldest rows as the most precise.

Nothing here recomputes a score. It reads what the runs stored, so a number on the
leaderboard can always be checked against the run file that produced it — which is
why the run id is on every row.
"""
import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import RUNS_DIR
from .evaluate import list_runs


@dataclass
class Group:
    """Rows that are comparable to one another, and nothing else."""
    question_ids: tuple
    judge_model: str
    judge_provider: str
    rows: list = field(default_factory=list)

    @property
    def n_questions(self) -> int:
        # From the ids rather than the row's own count: they are what makes two
        # rows comparable, and a stored count could disagree with them.
        if not _sample_recorded(self):
            return self.rows[0].get('n_questions', 0) if self.rows else 0
        return len(self.question_ids)

    @property
    def sample(self) -> str:
        balance = (self.rows[0].get('selection') or {}).get('balance', '') \
            if self.rows else ''
        if not _sample_recorded(self):
            return (f'{self.n_questions} questions, but *which* questions was '
                    'not recorded')
        return f'{self.n_questions} questions' + (f', {balance}' if balance else '')

    @property
    def newest(self) -> str:
        """The latest `started_at` on any row. Timestamps are zero-padded, so
        lexical order is chronological order."""
        return max((r.get('started_at', '') for r in self.rows), default='')

    @property
    def judge(self) -> str:
        """Reads after "judged by", so every branch has to be a noun phrase."""
        if self.judge_model:
            return f'{self.judge_model} via {self.judge_provider or "?"}'
        # A row carrying a decision score was judged by *something* — the runs
        # predating `report['judge']` simply did not write down what. "No judge"
        # would be a claim, and the wrong one; not knowing is the fact.
        if any(r.get('ragas_decision') is not None for r in self.rows):
            return 'a judge that was not recorded'
        return 'no judge — nothing on these rows was judged'


def _key(row: dict) -> tuple:
    selection = row.get('selection') or {}
    judge = row.get('judge') or {}
    # Sorted, because two runs that scored the same questions in a different
    # order are the same measurement. A frozenset would lose the count on a
    # duplicate id, which sorted() keeps.
    ids = tuple(sorted(selection.get('question_ids') or ()))
    if not ids:
        # Every run predating `RunResult.selection` has no ids, and keying on the
        # ids alone put 3-, 24- and 100-question runs in one ranked table — a
        # missing sample read as a *shared* one. Falling back to the count at
        # least stops that, and `verdict()` still refuses to call these groups,
        # because two runs of 24 may be two different 24.
        ids = ('n', row.get('n_questions', 0))
    return (ids, judge.get('model', ''), judge.get('provider', ''))


def _sample_recorded(found: 'Group') -> bool:
    return bool(found.question_ids) and found.question_ids[0] != 'n'


def group(rows: list[dict]) -> list[Group]:
    """Partition rows into comparability groups, each internally ranked.

    Rows with no decision score stay in their group and sort last: a run that
    could not measure all four deciding metrics is a fact about that run, and
    dropping it would hide an attempt rather than report it."""
    groups: dict[tuple, Group] = {}
    for row in rows:
        ids, model, provider = _key(row)
        found = groups.get((ids, model, provider))
        if found is None:
            found = Group(ids, model, provider)
            groups[(ids, model, provider)] = found
        found.rows.append(row)
    for found in groups.values():
        found.rows.sort(key=lambda r: (r.get('ragas_decision') is None,
                                       -(r.get('ragas_decision') or 0.0)))
    # Rankable groups first, then the merely listed ones. A reader opens this for
    # the live decision, and sorting by question count put the 100-question group
    # of unrecorded samples — which cannot be ranked at all — above the 30-question
    # group that decides something. Within each class: most recent first, since the
    # newest sample is the one still in use.
    #
    # Two stable passes rather than one key, because the date wants descending
    # order and the flags ascending, and a string cannot be negated.
    by_date = sorted(groups.values(), key=lambda g: g.newest, reverse=True)
    return sorted(by_date, key=lambda g: (not _sample_recorded(g),
                                          verdict(g) in ('unknown', 'unranked')))


def verdict(found: Group) -> str:
    """The label of the winning row, 'tie' if the lead is inside the error, or
    'unknown' if no error was measured.

    The error is the *combined* one over the two rows being compared, because the
    question being asked is whether their difference is distinguishable from
    zero — not whether either mean is precise."""
    if not _sample_recorded(found):
        # Two runs of 24 questions may be two *different* 24 — the striding rule
        # changed during this lab's life and nothing on those rows says which
        # questions they were. Equal counts are not a shared sample.
        return 'unknown'
    ranked = [r for r in found.rows if r.get('ragas_decision') is not None]
    if not ranked:
        return 'unknown'
    if len(ranked) == 1:
        # One measured row cannot beat anything; saying it won would make a
        # single run read as a comparison.
        return 'unranked'
    best, second = ranked[0], ranked[1]
    errors = [best.get('ragas_decision_stderr'),
              second.get('ragas_decision_stderr')]
    if any(error is None for error in errors):
        return 'unknown'
    combined = (errors[0] ** 2 + errors[1] ** 2) ** 0.5
    lead = best['ragas_decision'] - second['ragas_decision']
    return best['label'] if lead > combined else 'tie'


def _cell(value, digits: int = 3) -> str:
    return '—' if value is None else f'{value:.{digits}f}'


def markdown(groups: list[Group]) -> str:
    """The leaderboard as markdown, one table per comparability group."""
    out = ['# RAG lab leaderboard',
           '',
           'Generated by `npm run raglab:leaderboard` from `.runs/`. **One table per',
           'comparability group** — a decision score is a mean over questions judged',
           'by a model, so it is comparable only against rows that scored the same',
           'questions with the same judge. Rows are never ranked across tables.',
           '']
    for found in groups:
        out.append(f'## {found.sample} · judged by {found.judge}')
        out.append('')
        call = verdict(found)
        if call == 'tie':
            out.append('**No winner**: the lead is inside the combined error of the '
                       'top two rows, so these rows do not separate.')
        elif call == 'unknown' and not _sample_recorded(found):
            out.append('**Not comparable**: these runs did not record *which* '
                       'questions they scored, only how many. Equal counts are not '
                       'a shared sample — the sampling rule changed during this '
                       "lab's life — so the ordering below is a listing, not a "
                       'ranking.')
        elif call == 'unknown':
            out.append('**No winner**: these rows carry no measured error, so a lead '
                       'cannot be told from noise. Runs predating `decision_spread` '
                       'cannot have one reconstructed.')
        elif call == 'unranked':
            out.append('**One judged row only** — nothing to compare it against.')
        else:
            out.append(f'**Winner: {call}**, by more than the combined error of the '
                       'top two rows.')
        out.append('')
        out.append('| # | candidate | decision | ± | questions | seconds | run |')
        out.append('| --- | --- | --- | --- | --- | --- | --- |')
        for i, row in enumerate(found.rows, start=1):
            # A numbered row is a rank claim. A group whose sample was never
            # recorded is ordered for readability only, so it gets no numbers —
            # otherwise the table contradicts the sentence above it.
            rank = (str(i) if row.get('ragas_decision') is not None
                    and _sample_recorded(found) else '—')
            out.append(
                f'| {rank} | {row.get("label", "")} '
                f'| {_cell(row.get("ragas_decision"), 4)} '
                f'| {_cell(row.get("ragas_decision_stderr"), 3)} '
                f'| {row.get("n_questions", 0)} '
                f'| {round(row.get("seconds", 0))} '
                f'| `{row.get("run_id", "")}` |')
        out.append('')
    return '\n'.join(out)


def build(limit: int = 500) -> list[Group]:
    return group(list_runs(limit=limit))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=500,
                        help='how many run files to read (newest first)')
    parser.add_argument('--write', metavar='PATH',
                        help='write the markdown here instead of printing it')
    parser.add_argument('--json', action='store_true',
                        help='dump the grouped rows instead of markdown')
    args = parser.parse_args()
    groups = build(args.limit)
    if args.json:
        print(json.dumps([{'sample': g.sample, 'judge': g.judge,
                           'verdict': verdict(g), 'rows': g.rows}
                          for g in groups], ensure_ascii=False, indent=1))
        return
    text = markdown(groups)
    if args.write:
        path = Path(args.write)
        path.write_text(text + '\n', encoding='utf-8')
        print(f'wrote {path} · {len(groups)} comparability groups '
              f'from {sum(len(g.rows) for g in groups)} runs in {RUNS_DIR}')
    else:
        print(text)


if __name__ == '__main__':
    main()
