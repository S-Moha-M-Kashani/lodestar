"""Write a finished run out as one readable page per question.

The leaderboard says which architecture won. It cannot show what the pipeline
did to any single question, and that is what you need in order to argue about
whether the win is real — which questions it got right, what it retrieved, what
it said, and which number graded that.

**This module only reports what the run stored.** Two things follow from that,
and both are load-bearing:

- Runs record the retrieved *session ids*, not the chunk text. The page says so
  instead of re-running retrieval to reconstruct them: that would document a
  different retrieval than the one that was graded, against a possibly rebuilt
  index, and present it as the graded one.
- The four deciding RAGAS metrics are stored as run means, not per sample. They
  appear on each page labelled as run means, because an unlabelled 0.77 beside
  one question reads as that question's faithfulness — the most misleading thing
  this export could do.

"Answered correctly" also needs a definition, and the only one this data
supports is evidence-based rather than judged: an answerable question counts
when the pipeline did not refuse *and* reached a gold session; an unanswerable
one counts when it refused. Those are the two failures that matter — inventing
an answer that was not there, and refusing one that was.
"""
from pathlib import Path

from . import metrics

DIFFICULTIES = ('easy', 'medium', 'hard')

# The per-question fields that are grades, in the order a reader wants them:
# did retrieval reach the evidence, how well was it ranked, did the answer
# resemble the reference. `metrics.MEASURES` supplies each one's definition, so
# this list holds no wording of its own — a label that drifts from its
# definition explains the wrong number.
GRADE_KEYS = ('recall', 'quote_recall', 'ndcg', 'mrr', 'precision', 'hit',
              'false_abstention', 'answer_similarity', 'answer_token_f1',
              'key_fact_coverage', 'latency_ms')


def _num(value, places: int = 4) -> str:
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (int, float)):
        return f'{float(value):.{places}f}'.rstrip('0').rstrip('.')
    return str(value)


def _mean(values: list) -> float | None:
    kept = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(kept) / len(kept), 4) if kept else None


def answered_correctly(row: dict) -> bool:
    """Evidence-based correctness for one question.

    Answerable: it must not have refused, and retrieval must have reached at
    least one session the ground truth marks as evidence. Unanswerable: it must
    have refused. There is no judged per-question grade in a run file, so this
    deliberately claims less than "correct" — it is "answered from the right
    place, or rightly declined".
    """
    if not row.get('answerable', True):
        return bool(row.get('abstained'))
    return not row.get('abstained') and bool(row.get('hit'))


def difficulty_rates(rows: list[dict]) -> list[dict]:
    """One row per difficulty, in ascending order, with the count beside every
    share — one hard question at 100% is not a finding.

    `evidence_found` and `quotes_in_context` stay separate from `answered`
    because retrieval reaching the evidence and the answer using it are
    different failures, and collapsing them hides which half to fix. They are
    `None` where the difficulty holds only unanswerable questions: those have no
    evidence to find, and averaging them in as misses would understate
    retrieval.
    """
    out = []
    for name in DIFFICULTIES:
        group = [row for row in rows if row.get('difficulty') == name]
        if not group:
            continue
        answerable = [row for row in group if row.get('answerable', True)]
        out.append({
            'difficulty': name,
            'n': len(group),
            'n_answerable': len(answerable),
            'answered': round(
                sum(1 for row in group if answered_correctly(row)) / len(group), 4),
            'evidence_found': _mean([row.get('hit') for row in answerable]),
            'quotes_in_context': (
                round(sum(1 for row in answerable
                          if row.get('quote_recall') == 1.0) / len(answerable), 4)
                if answerable else None),
            'recall': _mean([row.get('recall') for row in answerable]),
            'answer_overlap': _mean([row.get('answer_token_f1')
                                     for row in group]),
        })
    return out


def type_rates(rows: list[dict]) -> list[dict]:
    """The same table by question type: an aggregate hides a change that helps
    one kind of question and hurts another."""
    out = []
    for name in dict.fromkeys(row.get('type') for row in rows):
        group = [row for row in rows if row.get('type') == name]
        out.append({
            'type': name, 'n': len(group),
            'answered': round(
                sum(1 for row in group if answered_correctly(row)) / len(group), 4),
            'recall': _mean([row.get('recall') for row in group]),
        })
    return out


def _table(header: list[str], body: list[list[str]]) -> str:
    lines = ['| ' + ' | '.join(header) + ' |',
             '| ' + ' | '.join('---' for _ in header) + ' |']
    lines += ['| ' + ' | '.join(cell for cell in row) + ' |' for row in body]
    return '\n'.join(lines)


def question_page(run: dict, question: dict, row: dict) -> str:
    """One question: what was asked, what the truth is, what came back, what it
    replied, and what graded it."""
    gold = {item['session_id'] for item in question.get('evidence', [])}
    measures = {measure.key: measure for measure in metrics.MEASURES}
    parts = [
        f"# {question['id']} — {question['type']} / {question['difficulty']}",
        '',
        f"Run `{run['run_id']}` · **{run.get('label', '')}** · "
        f"k={run['config']['retrieval'].get('k')} · "
        f"chunker `{run['config']['index'].get('chunker')}` · "
        f"answerer `{run['config']['generation'].get('answerer')}`",
        '',
        '## Asked',
        '',
        f"> {question['question_fa']}",
        '',
        f"*{question.get('question_en', '')}*",
        '',
        f"Answerable: **{'yes' if question.get('answerable') else 'no'}** · "
        f"query date {question.get('query_date', '')} · "
        f"declared time scope {question.get('time_scope') or '—'} · "
        f"detected time scope {row.get('time_scope') or '—'}",
        '',
        '## Reference',
        '',
    ]
    if question.get('answer_fa'):
        parts += [f"> {question['answer_fa']}", '']
    if question.get('key_facts'):
        parts += ['Key facts the answer has to contain:', '']
        parts += [f'- {fact}' for fact in question['key_facts']] + ['']
    if question.get('evidence'):
        parts += ['Evidence, with the sentence the ground truth cites:', '']
        parts += [f"- `{item['session_id']}` messages "
                  f"{item.get('message_indices', [])} — «{item['quote']}»"
                  for item in question['evidence']] + ['']
    else:
        parts += ['No evidence: this question is unanswerable from the diary, '
                  'and the correct response is a refusal.', '']

    retrieved = row.get('retrieved_sessions') or []
    parts += [
        '## Retrieved',
        '',
        f"{row.get('n_contexts', '—')} chunks · "
        f"{row.get('context_chars', '—')} characters · "
        f"{_num(row.get('latency_ms'), 1)} ms",
        '',
    ]
    if retrieved:
        parts += ['Sessions in rank order — ✓ marks one the ground truth cites:',
                  '']
        parts += [f"{i}. `{session}`{'  ✓' if session in gold else ''}"
                  for i, session in enumerate(retrieved, start=1)]
    else:
        parts += ['No session-bearing chunk was retrieved. Chunks carry '
                  'no session id, so a ledger or a digest can be the whole '
                  'context and leave this list empty.']
    parts += ['',
              '*The chunk text is not stored in a run file — only the session '
              'ids above. Reconstructing the chunks would mean re-running '
              'retrieval, which would document a different retrieval than the '
              'one that was graded.*',
              '',
              '## Answered',
              '']
    parts += [f"Refused: **{'yes' if row.get('abstained') else 'no'}**", '']
    parts += ['```', (row.get('answer') or '(no answer)').strip(), '```', '']

    parts += ['## Graded', '']
    body = []
    for key in GRADE_KEYS:
        if row.get(key) is None:
            continue
        measure = measures.get(key)
        body.append([
            measure.label if measure else key,
            f'`{key}`',
            _num(row[key]),
            f'`{measure.formula}`' if measure else '—',
            measure.library if measure else '—',
        ])
    body.append(['Answered from the right evidence', '`answered`',
                 'yes' if answered_correctly(row) else 'no',
                 '`(answerable ∧ ¬refused ∧ hit) ∨ (¬answerable ∧ refused)`',
                 'raglab.export.answered_correctly (no model)'])
    parts += [_table(['Grade', 'Key', 'Value', 'Formula', 'Computed by'], body), '']

    judged = (run.get('ragas') or {}).get('metrics') or {}
    if judged:
        parts += [
            '### The judged metrics are run means, not per question',
            '',
            'RAGAS scores every sample and the lab stores the averages, so the '
            'four numbers that chose this architecture cannot be attributed to '
            'this question. They are the whole run:',
            '',
        ]
        parts += [f'- `{key}` = {_num(value)} (run mean over '
                  f"{(run.get('ragas') or {}).get('n_samples', '?')} samples)"
                  for key, value in sorted(judged.items())]
        parts += ['']
    return '\n'.join(parts) + '\n'


def index_page(run: dict, rows: list[dict]) -> str:
    """The folder's own front page: how to read it, and the two rate tables."""
    spread = (run.get('ragas') or {}).get('decision_spread') or {}
    decision = (run.get('ragas') or {}).get('decision')
    parts = [
        '# Per-question record of the chosen configuration',
        '',
        f"Run `{run['run_id']}` · **{run.get('label', '')}** · "
        f"{len(rows)} questions · {run.get('seconds')}s",
        '',
        f"RAGAS decision score **{_num(decision)}**"
        + (f" ± {_num(spread.get('stderr'))} (standard error over "
           f"{spread.get('n')} questions)" if spread.get('stderr') is not None
           else ' (no measured spread)'),
        '',
        'One file per question, each showing what was asked, what the ground '
        'truth says, which sessions came back, what the pipeline replied, and '
        'every grade with its own arithmetic beside it.',
        '',
        '## What "answered" means here',
        '',
        'A run file stores no judged grade per question, so this column is '
        'evidence-based and deliberately claims less than "correct": an '
        'answerable question counts when the pipeline did **not** refuse and '
        'retrieval reached a session the ground truth cites; an unanswerable '
        'one counts when it **did** refuse. Inventing an answer that was not '
        'there and refusing one that was are the two failures that matter, and '
        'both are counted. Whether the wording of a non-refused answer is right '
        'is graded separately, by the four judged metrics — as run means only.',
        '',
        '## By difficulty',
        '',
        _table(['Difficulty', 'n', 'Answered', 'Evidence found',
                'Gold quote in context', 'Recall@k', 'Answer overlap'],
               [[row['difficulty'], str(row['n']),
                 f"**{row['answered'] * 100:.0f}%**",
                 _num(row['evidence_found']), _num(row['quotes_in_context']),
                 _num(row['recall']), _num(row['answer_overlap'])]
                for row in difficulty_rates(rows)]),
        '',
        '## By question type',
        '',
        _table(['Type', 'n', 'Answered', 'Recall@k'],
               [[row['type'], str(row['n']), f"{row['answered'] * 100:.0f}%",
                 _num(row['recall'])] for row in type_rates(rows)]),
        '',
        '## Questions',
        '',
    ]
    for row in rows:
        mark = '✓' if answered_correctly(row) else '·'
        parts.append(f"- {mark} [{row['id']}]({row['id']}.md) — "
                     f"{row.get('type')} / {row.get('difficulty')}")
    return '\n'.join(parts) + '\n'


def write_run(run: dict, ground_truth: dict, out_dir) -> list[Path]:
    """Write the index and one page per question. Returns the paths written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    questions = {q['id']: q for q in ground_truth['questions']}
    rows = [row for row in run['rows'] if row['id'] in questions]
    written = [out / 'README.md']
    written[0].write_text(index_page(run, rows), encoding='utf-8')
    for row in rows:
        path = out / f"{row['id']}.md"
        path.write_text(question_page(run, questions[row['id']], row),
                        encoding='utf-8')
        written.append(path)
    return written
