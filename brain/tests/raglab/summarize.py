"""The summary hierarchy: session → month → thread.

Leaf chunks answer "what did I say on the 10th of March". They cannot answer
"how often did I promise to go to the gym", "what was my worst stretch this
year", or "what actually happened with the tax file" — those need a document
that never existed in the conversation. So the lab builds three rollup layers
(RAPTOR's idea: index summaries alongside the raw text, not instead of it):

  session  one paragraph per session, indexed as its own document
  month    a chronological digest of one month's sessions
  thread   a chronological digest of one storyline across the whole year —
           the cross-cutting layer, and the only one that makes an aggregation
           or pattern question answerable in a single retrieval

Rollups are *additive*. Nothing replaces the verbatim text, because the
substitution is what loses information: a summary that drops "the sixth
rejection" makes the counting question unanswerable forever, while a summary
kept beside the original costs only storage.

Two summarizers: `extractive` is deterministic, offline, and free (salience by
corpus IDF), so CI can measure the hierarchy's value without a network call.
`llm` is the real thing, and its output is cached for the life of the process —
157 sessions × every sweep would otherwise be the dominant cost of the lab, and
switching a chunker changes every chunk while changing no summary.

**The cache is memory, not a file.** A summary is model output about
experimental data: derived, rebuildable, and worth nothing to anyone after the
run it fed. On disk it was also unattributable — a file with no record of which
model wrote which entry, silently feeding entries into later runs that never
asked for them. The cost of that decision is honest and worth stating: a fresh
process with `summarizer='llm'` pays for the corpus again.
"""
import math
import re
from collections import defaultdict

from lodestar_brain import textnorm
from .chunking import Chunk, importance_of
from .corpus import date_int
from .llm import lab_chat

SUMMARY_SENTENCES = 3
THREAD_LINES_PER_CHUNK = 14


def build_idf(sessions: list[dict]) -> dict[str, float]:
    """Document frequency over sessions, so 'salient' means rare-in-corpus
    rather than merely frequent — otherwise every summary is about مهسا."""
    df: dict[str, int] = defaultdict(int)
    for session in sessions:
        seen = {t for m in session['messages'] for t in textnorm.tokens(m['content'])}
        for token in seen:
            df[token] += 1
    total = len(sessions) or 1
    return {token: math.log(1 + total / count) for token, count in df.items()}


class ExtractiveSummarizer:
    """Pick the sentences that carry the session's rare content words.

    Only *user* turns are candidates: the coach's replies are reflections of
    what the diarist said, so summarising them double-counts the assistant's
    vocabulary and buries the facts."""
    name = 'extractive'

    def __init__(self, idf: dict[str, float]):
        self.idf = idf

    def _score(self, sentence: str) -> float:
        tokens = textnorm.tokens(sentence)
        if not tokens:
            return 0.0
        # length-normalised, so a run-on sentence does not win by volume alone
        return sum(self.idf.get(t, 0.0) for t in tokens) / math.sqrt(len(tokens))

    def session(self, session: dict) -> str:
        candidates: list[str] = []
        for message in session['messages']:
            if message['role'] == 'user':
                candidates.extend(s for s in textnorm.sentences(message['content'])
                                  if len(s) > 20)
        if not candidates:
            candidates = [session['messages'][0]['content'][:200]]
        # The opener always survives: diary entries state their subject first.
        keep = {0}
        ranked = sorted(range(len(candidates)), key=lambda i: -self._score(candidates[i]))
        for i in ranked:
            if len(keep) >= SUMMARY_SENTENCES:
                break
            keep.add(i)
        picked = [candidates[i] for i in sorted(keep)]
        return ' '.join(picked)


class LLMSummarizer:
    """Real abstractive summaries. Degrades to the extractive summarizer per
    session on any provider error — a half-built hierarchy would silently
    change what a run is measuring, and a wrong number is worse than a slow
    one."""
    PROMPT = (
        'این یک نشست از دفترچه روزانه یک کاربر با دستیارش است. در دو تا سه جمله '
        'فارسی خلاصه کن: چه اتفاقی افتاد، چه حسی داشت، چه تصمیم یا قولی داد. '
        'اسم‌ها، تاریخ‌ها و عددها را دقیق نگه دار. هیچ چیزی اضافه نکن.')

    def __init__(self, llm, model: str, fallback: ExtractiveSummarizer):
        self.llm = llm
        self.model = model
        self.fallback = fallback
        self.failures = 0

    @property
    def name(self) -> str:
        """The cache key carries the model, so switching summary models does not
        serve one model's summaries as the other's. Without this, comparing two
        summarisers silently compares whichever one ran first."""
        return f'llm:{self.model}' if self.model else 'llm'

    def session(self, session: dict) -> str:
        from .corpus import session_text
        try:
            turn = lab_chat(
                self.llm,
                [{'role': 'system', 'content': self.PROMPT},
                 {'role': 'user', 'content':
                  f"تاریخ {session['date']}، حال: {session['mood']['label']}\n"
                  f'{session_text(session)}'}],
                self.model)
            text = (turn.content or '').strip()
            if text:
                return text
        except Exception:
            pass
        self.failures += 1
        return self.fallback.session(session)


class SummaryCache:
    """In-memory cache keyed by summarizer + session id + a hash of the session
    text, so a corpus edit invalidates exactly the entries it changed and two
    summarizers never read each other's work.

    Owned by whoever wants the reuse — `IndexRegistry` holds one for its
    process, so a sweep of chunkers summarises the corpus once. A build handed no
    cache simply summarises what it needs; each session is summarised once per
    build either way."""

    def __init__(self):
        self.data: dict[str, str] = {}

    @staticmethod
    def key(summarizer_name: str, session: dict) -> str:
        from hashlib import blake2b
        from .corpus import session_text
        digest = blake2b(session_text(session).encode(), digest_size=6).hexdigest()
        return f"{summarizer_name}:{session['session_id']}:{digest}"

    def get(self, summarizer_name: str, session: dict) -> str | None:
        return self.data.get(self.key(summarizer_name, session))

    def put(self, summarizer_name: str, session: dict, summary: str) -> None:
        self.data[self.key(summarizer_name, session)] = summary


def session_summaries(sessions: list[dict], summarizer,
                      cache: SummaryCache | None = None,
                      progress=None) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, session in enumerate(sessions):
        cached = cache.get(summarizer.name, session) if cache else None
        if cached is None:
            cached = summarizer.session(session)
            if cache:
                cache.put(summarizer.name, session, cached)
        out[session['session_id']] = cached
        if progress and i % 10 == 0:
            progress(i, len(sessions))
    return out


def _one_liner(summary: str, limit: int = 160) -> str:
    first = (textnorm.sentences(summary) or [summary])[0]
    return first if len(first) <= limit else first[:limit].rstrip() + '…'


def session_layer(sessions: list[dict], summaries: dict[str, str]) -> list[Chunk]:
    """L1: each session summary as its own retrievable document, headed by the
    facts a summary sentence never repeats (date, mood, storyline)."""
    out = []
    for session in sessions:
        sid = session['session_id']
        threads = '، '.join(session['recurring_threads']) or '—'
        text = (f"خلاصه نشست {session['date']} (حال: {session['mood']['label']}، "
                f"رشته‌ها: {threads})\n{summaries[sid]}")
        di = date_int(session['date'])
        out.append(Chunk(id=f'{sid}:summary', text=text, layer='session',
                         session_id=sid, date=session['date'], span_from=di,
                         span_to=di, time=session['time'], source=session['source'],
                         mood=session['mood']['label'],
                         valence=session['mood']['valence'],
                         arousal=session['mood']['arousal'],
                         importance=importance_of(session),
                         topics=tuple(session['topics']),
                         threads=tuple(session['recurring_threads']),
                         msg_start=0, msg_end=len(session['messages']) - 1))
    return out


def _digest_chunk(chunk_id: str, layer: str, title: str, lines: list[tuple[str, str]],
                  threads: tuple[str, ...]) -> Chunk:
    body = '\n'.join(f'- {date}: {line}' for date, line in lines)
    dates = [date_int(d) for d, _ in lines]
    return Chunk(id=chunk_id, text=f'{title}\n{body}', layer=layer,
                 date=lines[0][0], span_from=min(dates), span_to=max(dates),
                 threads=threads, importance=0.5)


def month_layer(sessions: list[dict], summaries: dict[str, str]) -> list[Chunk]:
    """L2: one digest per calendar month. Answers 'how was March' without
    retrieving fourteen sessions, and gives temporal questions a document whose
    date range is the month itself."""
    by_month: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        by_month[session['date'][:7]].append(session)
    out = []
    for month, group in sorted(by_month.items()):
        group.sort(key=lambda s: s['date'])
        moods = [s['mood']['valence'] for s in group]
        threads = sorted({t for s in group for t in s['recurring_threads']})
        title = (f'مرور ماه {month}: {len(group)} نشست، میانگین حال '
                 f'{sum(moods) / len(moods):.1f} از ۱۰، رشته‌ها: '
                 f"{'، '.join(threads) or '—'}")
        lines = [(s['date'], _one_liner(summaries[s['session_id']])) for s in group]
        out.append(_digest_chunk(f'month-{month}', 'month', title, lines,
                                 tuple(threads)))
    return out


def thread_layer(sessions: list[dict], summaries: dict[str, str],
                 thread_meta: dict[str, str]) -> list[Chunk]:
    """L3: one chronological digest per storyline, split into windows.

    This is the layer that makes 'چند بار قول دادم' and 'کِی تموم شد' single-hop
    questions. It is windowed rather than one document per thread because
    job-search spans 58 sessions — one 10 KB document embeds to mush, and no
    reranker can recover from that."""
    by_thread: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        for slug in session['recurring_threads']:
            by_thread[slug].append(session)
    out = []
    for slug, group in sorted(by_thread.items()):
        group.sort(key=lambda s: s['date'])
        lines = [(s['date'], _one_liner(summaries[s['session_id']])) for s in group]
        windows = [lines[i:i + THREAD_LINES_PER_CHUNK]
                   for i in range(0, len(lines), THREAD_LINES_PER_CHUNK)] or [[]]
        for i, window in enumerate(windows):
            if not window:
                continue
            part = f' (بخش {i + 1} از {len(windows)})' if len(windows) > 1 else ''
            title = (f'رشته «{slug}»{part} — {len(group)} نشست از '
                     f'{lines[0][0]} تا {lines[-1][0]}\n'
                     f"شرح: {thread_meta.get(slug, '')}")
            out.append(_digest_chunk(f'thread-{slug}-{i}', 'thread', title,
                                     window, (slug,)))
    return out


def commitment_layer(sessions: list[dict]) -> list[Chunk]:
    """L4: the promise ledger — every commitment-shaped sentence of the year in
    one chronological document, windowed like the thread layer.

    Commitments are the diary's highest-value fact type (they are what
    follow-through is measured against) and the hardest to retrieve from raw
    chunks, because «از فردا میرم باشگاه» is four words inside a long ramble
    about something else entirely."""
    lines = [(date, f'{sentence}  [{sid}]')
             for date, sid, sentence in commitment_lines(sessions)]
    windows = [lines[i:i + THREAD_LINES_PER_CHUNK * 2]
               for i in range(0, len(lines), THREAD_LINES_PER_CHUNK * 2)]
    out = []
    for i, window in enumerate(windows):
        if not window:
            continue
        title = (f'دفتر قول‌ها و ددلاین‌ها (بخش {i + 1} از {len(windows)}) — '
                 f'جمله‌هایی که کاربر در آن‌ها قول یا تصمیمی داده')
        out.append(_digest_chunk(f'commitments-{i}', 'commitment', title, window, ()))
    return out


HABIT_PERIOD_FA = {'daily': 'روز', 'weekly': 'هفته', 'monthly': 'ماه',
                   'yearly': 'سال'}


def habit_layer(sessions: list[dict], habits: dict) -> list[Chunk]:
    """L5: one adherence ledger per habit — the diary equivalent of the board's
    punch card, flattened into a document.

    `sessions` is accepted and unused: every layer builder takes it first, and
    this one happens to need only the declared history. Keeping the shape means
    `index.py` calls all six the same way. The obvious use for it — citing the
    check-in session beside each completion, the way the commitment layer does —
    is left alone deliberately, because a completion date is not necessarily a
    session date and guessing the link would put wrong citations in a ledger.

    A habit is the one card type you never finish, so the question asked of it is
    never "what happened" but "how often, against what target, and is it still
    going". None of the other layers can answer that. The raw chunks hold single
    check-ins scattered over fifty sessions; the session and month summaries
    average them away; the thread digest is chronological prose with no
    arithmetic in it. Counting across fifty retrieved chunks is exactly what a
    language model is worst at, and it is free to do here, once, at index time.

    So the ledger states the *target* beside every tally — a bare "18 times"
    means nothing without "3 per week" — names the periods that fell short, and
    ends on the date of the last completion, because "am I still doing this" is
    answered by an absence that no amount of retrieved text contains.

    Additive like every other rollup: the check-in sessions stay indexed as
    themselves, so "what did I say the week I quit" is still answerable.
    """
    out: list[Chunk] = []
    for slug, habit in sorted(habits.items()):
        history = habit.get('history') or {}
        target = habit['count']
        unit = HABIT_PERIOD_FA.get(habit['freq'], habit['freq'])
        periods = sorted(history)
        days = sorted(d for group in history.values() for d in group)
        if not days:
            continue
        full = [p for p in periods if len(history[p]) >= target]
        short = [p for p in periods if len(history[p]) < target]
        title = (f"دفتر عادت «{habit['title_fa']}» — هدف: {target} بار در هر "
                 f"{unit}؛ از {habit.get('started') or days[0]} تا {days[-1]}؛ "
                 f'مجموع {len(days)} بار در {len(periods)} {unit}؛ '
                 f'{len(full)} {unit} کامل و {len(short)} {unit} ناقص')
        rows = [f"- {p}: {len(history[p])} از {target}"
                f"{'  ✓' if len(history[p]) >= target else ''}"
                f"  ({'، '.join(history[p])})" for p in periods]
        body = '\n'.join(rows)
        note = habit.get('note_fa', '')
        text = '\n'.join(filter(None, [title, note, body,
                                       f'آخرین بار: {days[-1]}']))
        out.append(Chunk(
            id=f'habit-{slug}', text=text, layer='habit',
            date=days[0], span_from=date_int(days[0]), span_to=date_int(days[-1]),
            threads=tuple(filter(None, [habit.get('thread')])),
            # The one new metadata field, so a query can ask for one habit's
            # ledger by name instead of hoping the slug survived embedding.
            habit=slug,
            # Habits are the diary's follow-through record, which is what the
            # board exists to drive — never small talk, whatever the mood was.
            importance=0.8))
    return out


def commitment_lines(sessions: list[dict]) -> list[tuple[str, str, str]]:
    """Promise-shaped sentences, found by the phrasings this diarist actually
    uses («از فردا میرم باشگاه», «قول میدم», «باید تا جمعه»).

    Deliberately a regex and not an LLM: a commitment ledger is only worth
    indexing if it is cheap enough to rebuild on every ingest, and the recall of
    these six patterns on this corpus is high because promises are formulaic."""
    patterns = re.compile(
        r'(قول (?:می ?دم|دادم|میدم)|از فردا|از هفته (?:بعد|دیگه)|از شنبه|'
        r'تصمیم گرفتم|دیگه (?:نمی|نمیخوام)|باید تا |سر وقت|ددلاین)')
    found = []
    for session in sessions:
        for message in session['messages']:
            if message['role'] != 'user':
                continue
            for sentence in textnorm.sentences(message['content']):
                if patterns.search(sentence):
                    found.append((session['date'], session['session_id'], sentence))
    return found
