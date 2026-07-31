"""Chunking strategies for spoken diary text, plus the metadata every chunk
carries into the index.

The corpus is what makes this hard: a voice session rambles, switches topic
mid-message («... حالا اینا رو ولش کن، امروز مهسا...»), and the thing worth
retrieving a year later is usually one clause inside a 900-character monologue.
Six strategies are offered because they fail differently, and the lab exists to
show which failure costs least:

  fixed          the brain's current behaviour (greedy 500-char packing) — the
                 baseline; splits mid-thought and drops speaker attribution
  fixed-overlap  the same with a sliding overlap, so a thought cut in half is
                 whole in one of the two windows
  message        one chunk per turn: precise, but a short «آره همون» is
                 unretrievable on its own
  turn-pair      user turn + the coach's reply: keeps the interpretation with
                 the raw feeling
  session        one chunk per session: maximum fidelity, minimum precision
  semantic-drift topic segmentation — cut where consecutive messages stop being
                 about the same thing (embedding drift + Farsi discourse markers)

Orthogonal to all six: `contextual`. Anthropic's contextual-retrieval result
(chunks prefixed with a short situating header retrieve markedly better) applies
directly here, because a diary chunk read cold has no date, no mood, and no
subject — «بلاخره جواب داد» is meaningless until you know it is about the tax
office, in January. The header is built from metadata plus the session summary,
so it costs no LLM call.
"""
from dataclasses import dataclass, field

import numpy as np

from lodestar_brain.rag.chat_memory import chunk_text

from . import textnorm
from .corpus import date_int, session_text

# Phrases the diarist actually uses when he abandons one subject for another.
# A hard cut here is cheap and catches shifts the embedding drift misses.
SHIFT_MARKERS = ('ولش کن', 'بذریم', 'بگذریم', 'راستی', 'یه چیز دیگه',
                 'در کل', 'یادم رفت بگم', 'اینا رو ولش', 'حالا اینا')


@dataclass
class Chunk:
    id: str
    text: str
    layer: str                  # chunk | session | month | thread | commitment | habit
    session_id: str = ''
    date: str = ''
    span_from: int = 0          # date_int; equals span_to for leaf chunks
    span_to: int = 0
    time: str = ''
    source: str = ''
    mood: str = ''
    valence: int = 0
    arousal: int = 0
    importance: float = 0.0
    topics: tuple[str, ...] = ()
    threads: tuple[str, ...] = ()
    habit: str = ''             # the habit slug a ledger chunk belongs to
    msg_start: int = -1
    msg_end: int = -1
    prefix: str = ''            # the contextual header, kept separately so
                                # metrics can measure the body alone

    def metadata(self) -> dict:
        """Flat and filterable. Lists become space-joined strings: the store can only
        filter on scalars, and a JSON blob would not be filterable either — the
        fields we actually filter on (span_from/span_to/layer/session_id/habit)
        are scalars by design.

        `habit` is present on every chunk, empty on the ones that belong to no
        habit: a field only some rows carry turns a `where` clause into a silent
        partial scan, which reads as a retrieval bug rather than a schema one.
        """
        return {
            'layer': self.layer, 'session_id': self.session_id, 'date': self.date,
            'span_from': self.span_from, 'span_to': self.span_to,
            'time': self.time, 'source': self.source, 'mood': self.mood,
            'valence': self.valence, 'arousal': self.arousal,
            'importance': self.importance,
            'topics': ' '.join(self.topics), 'threads': ' '.join(self.threads),
            'habit': self.habit,
            'msg_start': self.msg_start, 'msg_end': self.msg_end,
            'chars': len(self.text),
        }

    @property
    def body(self) -> str:
        """The chunk without its contextual header."""
        return self.text[len(self.prefix):] if self.prefix else self.text


def importance_of(session: dict) -> float:
    """Emotional intensity as a memorability proxy — the 'importance' term from
    Generative Agents, which the lab's `agentic` reranker weights alongside
    relevance and recency. High arousal *or* an extreme valence in either
    direction marks a session the diarist would still remember."""
    mood = session['mood']
    arousal = mood['arousal'] / 10.0
    extremity = abs(mood['valence'] - 5.5) / 4.5
    return round(min(1.0, 0.6 * arousal + 0.4 * extremity), 3)


def _speaker(role: str) -> str:
    return 'کاربر' if role == 'user' else 'دستیار'


def _base(session: dict, layer: str = 'chunk') -> dict:
    di = date_int(session['date'])
    return dict(layer=layer, session_id=session['session_id'], date=session['date'],
                span_from=di, span_to=di, time=session['time'],
                source=session['source'], mood=session['mood']['label'],
                valence=session['mood']['valence'], arousal=session['mood']['arousal'],
                importance=importance_of(session),
                topics=tuple(session['topics']),
                threads=tuple(session['recurring_threads']))


def contextual_prefix(session: dict, summary: str = '') -> str:
    """A two-line header naming when this was said, how it felt, and what the
    session was about. Deliberately short: it is duplicated into every chunk of
    the session, so anything longer starts to dominate the embedding."""
    threads = '، '.join(session['recurring_threads']) or '—'
    head = (f"[{session['date']} | حال: {session['mood']['label']} | "
            f"موضوع: {'، '.join(session['topics']) or '—'} | رشته: {threads}]")
    gist = textnorm.sentences(summary)
    if gist:
        head += f"\n(خلاصه‌ی این نشست: {gist[0]})"
    return head + '\n'


# --- leaf strategies -------------------------------------------------------

def _windows(text: str, size: int, overlap: int) -> list[str]:
    """Sliding character windows snapped to word boundaries."""
    if overlap >= size:
        overlap = size // 2
    step = max(1, size - overlap)
    words = text.split()
    out, i = [], 0
    while i < len(words):
        window, j = '', i
        while j < len(words) and len(window) + len(words[j]) + 1 <= size:
            window = f'{window} {words[j]}' if window else words[j]
            j += 1
        out.append(window)
        if j >= len(words):
            break
        # advance by roughly `step` characters, never by zero words
        consumed, k = 0, i
        while k < j - 1 and consumed + len(words[k]) + 1 < step:
            consumed += len(words[k]) + 1
            k += 1
        i = max(i + 1, k)
    return out


def _semantic_segments(session: dict, embedder, max_chars: int) -> list[list[int]]:
    """Group consecutive messages into topical segments.

    Boundary rule: cut where the cosine similarity between neighbouring messages
    falls into the bottom of *this session's* distribution (a relative threshold
    — an absolute one is meaningless across embedders whose scales differ by an
    order of magnitude), or where the diarist says out loud that he is changing
    subject, or where the segment would outgrow max_chars."""
    messages = session['messages']
    if len(messages) <= 2:
        return [list(range(len(messages)))]
    vectors = embedder.embed([m['content'] for m in messages])
    sims = np.array([float(vectors[i] @ vectors[i + 1])
                     for i in range(len(messages) - 1)], dtype=np.float32)
    cut_at = float(np.percentile(sims, 35)) if sims.size else -1.0
    segments, current, size = [], [0], len(messages[0]['content'])
    for i in range(1, len(messages)):
        marker = any(m in textnorm.normalize(messages[i]['content'])
                     for m in SHIFT_MARKERS)
        too_big = size + len(messages[i]['content']) > max_chars * 2
        drifted = sims[i - 1] <= cut_at
        if current and (marker or too_big or drifted):
            segments.append(current)
            current, size = [i], len(messages[i]['content'])
        else:
            current.append(i)
            size += len(messages[i]['content'])
    if current:
        segments.append(current)
    return segments


def chunk_session(session: dict, cfg, embedder, summary: str = '') -> list[Chunk]:
    """One session → leaf chunks, per the configured strategy."""
    base = _base(session)
    prefix = contextual_prefix(session, summary) if cfg.contextual else ''
    messages = session['messages']
    out: list[Chunk] = []

    def emit(text: str, i: int, start: int, end: int) -> None:
        out.append(Chunk(id=f"{session['session_id']}:c{i}", text=prefix + text,
                         prefix=prefix, msg_start=start, msg_end=end, **base))

    if cfg.chunker == 'session':
        emit(session_text(session), 0, 0, len(messages) - 1)
    elif cfg.chunker == 'message':
        for i, m in enumerate(messages):
            emit(f"{_speaker(m['role'])}: {m['content']}", i, i, i)
    elif cfg.chunker == 'turn-pair':
        i = 0
        while i < len(messages):
            group = [messages[i]]
            end = i
            if (messages[i]['role'] == 'user' and i + 1 < len(messages)
                    and messages[i + 1]['role'] == 'assistant'):
                group.append(messages[i + 1])
                end = i + 1
            emit('\n'.join(f"{_speaker(m['role'])}: {m['content']}" for m in group),
                 len(out), i, end)
            i = end + 1
    elif cfg.chunker == 'semantic-drift':
        for i, segment in enumerate(_semantic_segments(session, embedder,
                                                       cfg.chunk_chars)):
            emit('\n'.join(f"{_speaker(messages[j]['role'])}: "
                           f"{messages[j]['content']}" for j in segment),
                 i, segment[0], segment[-1])
    elif cfg.chunker == 'fixed-overlap':
        for i, piece in enumerate(_windows(session_text(session), cfg.chunk_chars,
                                           cfg.overlap)):
            emit(piece, i, -1, -1)
    else:   # 'fixed' — the production baseline, called rather than reimplemented
        for i, piece in enumerate(chunk_text(session_text(session), cfg.chunk_chars)):
            emit(piece, i, -1, -1)
    return out
