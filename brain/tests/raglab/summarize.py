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
`llm` is the real thing, cached on disk because 157 sessions × every sweep would
otherwise be the dominant cost of the lab.
"""
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from . import textnorm
from .chunking import Chunk, importance_of
from .config import RUNS_DIR
from .corpus import date_int

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
    name = 'llm'
    PROMPT = (
        'این یک نشست از دفترچه روزانه یک کاربر با دستیارش است. در دو تا سه جمله '
        'فارسی خلاصه کن: چه اتفاقی افتاد، چه حسی داشت، چه تصمیم یا قولی داد. '
        'اسم‌ها، تاریخ‌ها و عددها را دقیق نگه دار. هیچ چیزی اضافه نکن.')

    def __init__(self, llm, model: str, fallback: ExtractiveSummarizer):
        self.llm = llm
        self.model = model
        self.fallback = fallback
        self.failures = 0

    def session(self, session: dict) -> str:
        from .corpus import session_text
        try:
            turn = self.llm.chat(
                [{'role': 'system', 'content': self.PROMPT},
                 {'role': 'user', 'content':
                  f"تاریخ {session['date']}، حال: {session['mood']['label']}\n"
                  f'{session_text(session)}'}],
                model=self.model)
            text = (turn.content or '').strip()
            if text:
                return text
        except Exception:
            pass
        self.failures += 1
        return self.fallback.session(session)


class SummaryCache:
    """Disk cache keyed by summarizer + session id + a hash of the session text,
    so editing the fixture invalidates exactly the entries it changed."""

    def __init__(self, path: Path | None = None):
        # Under .runs/cache/, not .runs/ itself: the run listing globs *.json
        # there, and a cache file sitting beside the runs is not a run.
        self.path = path or RUNS_DIR / 'cache' / 'summary-cache.json'
        self.data: dict[str, str] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
            except Exception:
                self.data = {}
        self.dirty = False

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
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False),
                             encoding='utf-8')
        self.dirty = False


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
    if cache:
        cache.flush()
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
