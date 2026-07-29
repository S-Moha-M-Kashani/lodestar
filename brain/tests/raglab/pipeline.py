"""One question, end to end: understand → retrieve → fuse → rerank → grade →
assemble → answer, with every stage's output kept for the panel to show.

The diagnostics are not decoration. When a configuration scores badly the only
useful question is *which stage lost the evidence* — it was never retrieved, or
it was retrieved and reranked away, or it was graded out. A pipeline that only
returns its final answer cannot tell you, and tuning it becomes guesswork.
"""
import time
from dataclasses import dataclass, field

import numpy as np

from . import query as query_mod
from . import retrieval, textnorm
from .config import LAYERS, GenerationConfig, RetrievalConfig

REFUSAL = 'تو دفترچه چیزی دربارهٔ این پیدا نکردم.'
# Phrases only a *refusing assistant* produces. «نمیدونم» is deliberately absent:
# the diarist says it constantly, so counting it made every answer that quoted
# him look like an abstention — 6.5% of answerable questions were scored as
# refused by a pipeline with no gate switched on at all.
REFUSAL_MARKERS = ('پیدا نکردم', 'چیزی ثبت نشده', 'اطلاعاتی ندارم',
                   'در دفترچه نیست', 'در یادداشت‌ها نیست', 'موجود نیست',
                   'اشاره‌ای نشده', 'اشاره ای نشده', 'ذکر نشده', 'ثبت نشده')


@dataclass
class Context:
    chunk_id: str
    text: str
    layer: str
    session_id: str
    date: str
    score: float
    stages: dict = field(default_factory=dict)
    expanded_from: str = ''

    def as_dict(self) -> dict:
        return {'chunk_id': self.chunk_id, 'layer': self.layer,
                'session_id': self.session_id, 'date': self.date,
                'score': round(self.score, 4), 'text': self.text,
                'stages': {k: round(v, 4) for k, v in self.stages.items()},
                'expanded_from': self.expanded_from}


@dataclass
class Outcome:
    question: str
    contexts: list[Context]
    abstained: bool = False
    answer: str | None = None
    time_scope: dict | None = None
    diagnostics: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)

    @property
    def sessions(self) -> list[str]:
        """Distinct sessions represented in the context, best first — the unit
        the ground truth cites evidence in."""
        out: list[str] = []
        for context in self.contexts:
            if context.session_id and context.session_id not in out:
                out.append(context.session_id)
        return out

    def as_dict(self) -> dict:
        return {'question': self.question, 'abstained': self.abstained,
                'answer': self.answer, 'time_scope': self.time_scope,
                'contexts': [c.as_dict() for c in self.contexts],
                'sessions': self.sessions, 'diagnostics': self.diagnostics,
                'timings': self.timings}


def _mask(index, scope, layers: tuple[str, ...]) -> np.ndarray:
    """The BM25 equivalent of the Chroma `where` clause. Kept in lockstep with
    query.where_clause: if the two disagree, hybrid fusion silently compares two
    different candidate pools."""
    allowed = np.ones(len(index.chunks), dtype=bool)
    for i, chunk in enumerate(index.chunks):
        if chunk.layer not in layers:
            allowed[i] = False
        elif scope and (chunk.span_from > scope.to_int
                        or chunk.span_to < scope.from_int):
            allowed[i] = False
    return allowed


def lexical_grade(index, question: str, text: str) -> float:
    """IDF-weighted coverage: what share of the question's informative words
    this text actually contains. Bounded to [0,1], so it can be thresholded — a
    raw BM25 score cannot, because its scale depends on the corpus."""
    tokens = [t for t in textnorm.tokens(question)
              if t not in query_mod.QUESTION_WORDS]
    if not tokens:
        return 0.0
    present = set(textnorm.tokens(text))
    weights = {t: index.bm25.idf.get(t, 1.0) for t in set(tokens)}
    total = sum(weights.values()) or 1.0
    return float(sum(w for t, w in weights.items() if t in present) / total)


def retrieve(index, cfg: RetrievalConfig, question: str, query_date: str,
             llm=None, model: str = '') -> Outcome:
    timings: dict = {}
    diagnostics: dict = {}
    clock = time.perf_counter

    start = clock()
    scope = (query_mod.resolve_time_scope(question, query_date)
             if cfg.time_filter else None)
    queries = query_mod.expand(question) if cfg.multi_query else [question]
    if cfg.hyde and llm is not None:
        queries = queries + [query_mod.hyde(llm, model, question)]
    where = query_mod.where_clause(scope, cfg.search_layers, LAYERS)
    allowed = _mask(index, scope, cfg.search_layers)
    timings['understand_ms'] = round((clock() - start) * 1000, 1)
    diagnostics['queries'] = queries
    diagnostics['candidates_in_scope'] = int(allowed.sum())

    start = clock()
    dense_ranked: list[str] = []
    dense_scores: dict[str, float] = {}
    if cfg.retriever in ('dense', 'hybrid-rrf'):
        vectors = index.embedder.embed(queries)
        for chunk_id, score in index.dense(vectors, cfg.candidates, where):
            dense_scores[chunk_id] = score
            dense_ranked.append(chunk_id)
    lexical_ranked: list[str] = []
    lexical_scores: dict[str, float] = {}
    if cfg.retriever in ('bm25', 'hybrid-rrf'):
        merged: dict[int, float] = {}
        for text in queries:
            for doc_id, score in index.bm25.top(text, cfg.candidates, allowed):
                merged[doc_id] = max(merged.get(doc_id, 0.0), score)
        for doc_id, score in sorted(merged.items(), key=lambda kv: -kv[1]):
            chunk_id = index.chunks[doc_id].id
            lexical_scores[chunk_id] = score
            lexical_ranked.append(chunk_id)
    timings['retrieve_ms'] = round((clock() - start) * 1000, 1)
    diagnostics['dense_hits'] = len(dense_ranked)
    diagnostics['bm25_hits'] = len(lexical_ranked)

    if cfg.retriever == 'dense':
        base = {cid: dense_scores[cid] for cid in dense_ranked}
    elif cfg.retriever == 'bm25':
        base = {cid: lexical_scores[cid] for cid in lexical_ranked}
    else:
        base = retrieval.rrf([dense_ranked, lexical_ranked], cfg.rrf_k)
    if not base:
        return Outcome(question=question, contexts=[], abstained=True,
                       time_scope=scope.as_dict() if scope else None,
                       diagnostics=diagnostics | {'reason': 'no candidates'},
                       timings=timings)

    # The boost is applied to the fused scores, *before* the candidate cut. Doing
    # it afterwards is useless: there are twenty times more leaf chunks than
    # rollups, so a summary that did not already survive the cut can never be
    # promoted into it.
    if cfg.rollup_boost != 1.0:
        for chunk_id in list(base):
            chunk = index.by_id.get(chunk_id)
            if chunk is not None and chunk.layer != 'chunk':
                base[chunk_id] *= cfg.rollup_boost

    ids = sorted(base, key=lambda cid: -base[cid])[:max(cfg.rerank_depth, cfg.k)]
    chunks = [index.by_id[cid] for cid in ids if cid in index.by_id]
    ids = [c.id for c in chunks]
    relevance = retrieval.normalize_scores(
        np.array([base[cid] for cid in ids], dtype=np.float32))
    stage_scores = {cid: {'retrieval': float(r)} for cid, r in zip(ids, relevance)}

    start = clock()
    final = _rerank(index, cfg, question, chunks, relevance, query_date,
                    stage_scores, llm, model)
    timings['rerank_ms'] = round((clock() - start) * 1000, 1)

    start = clock()
    vectors = (index.vectors_for(ids) if cfg.mmr_lambda < 1.0
               else np.zeros((0, 1), dtype=np.float32))
    order = retrieval.mmr(vectors, final, cfg.k, cfg.mmr_lambda)
    timings['diversify_ms'] = round((clock() - start) * 1000, 1)

    contexts = []
    for i in order:
        chunk = chunks[i]
        contexts.append(Context(chunk_id=chunk.id, text=chunk.text, layer=chunk.layer,
                                session_id=chunk.session_id, date=chunk.date,
                                score=float(final[i]),
                                stages=stage_scores[chunk.id]))

    start = clock()
    kept, abstained = _grade(index, cfg, question, contexts, llm, model)
    timings['grade_ms'] = round((clock() - start) * 1000, 1)
    diagnostics['graded_out'] = len(contexts) - len(kept)

    kept = _expand_parents(index, cfg, kept)
    kept = _fit_budget(kept, cfg.max_context_chars)
    return Outcome(question=question, contexts=kept, abstained=abstained,
                   time_scope=scope.as_dict() if scope else None,
                   diagnostics=diagnostics, timings=timings)


def _rerank(index, cfg, question, chunks, relevance, query_date, stage_scores,
            llm, model) -> np.ndarray:
    if cfg.reranker == 'none' or not chunks:
        return relevance
    query_int = int(query_date.replace('-', ''))
    if cfg.reranker == 'lexical':
        scores = np.array([lexical_grade(index, question, c.text) for c in chunks],
                          dtype=np.float32)
        final = 0.5 * relevance + 0.5 * retrieval.normalize_scores(scores)
        key = 'lexical'
    elif cfg.reranker == 'recency':
        weights = np.array([retrieval.recency_weight(c.span_to, query_int,
                                                     cfg.recency_half_life_days)
                            for c in chunks], dtype=np.float32)
        final = relevance * weights
        scores, key = weights, 'recency'
    elif cfg.reranker == 'agentic':
        # Generative Agents' retrieval function: relevance + recency +
        # importance. The diary equivalent of "what would this person actually
        # recall when asked".
        wr, wt, wi = cfg.agentic_weights
        recency = np.array([retrieval.recency_weight(c.span_to, query_int,
                                                     cfg.recency_half_life_days)
                            for c in chunks], dtype=np.float32)
        importance = np.array([c.importance for c in chunks], dtype=np.float32)
        final = wr * relevance + wt * recency + wi * importance
        scores, key = final, 'agentic'
    elif cfg.reranker == 'cross-encoder':
        try:
            encoder = _cross_encoder(index)
            scores = encoder.score(question, [c.text for c in chunks])
            final = retrieval.normalize_scores(scores)
        except Exception:
            return relevance
        key = 'cross_encoder'
    else:   # 'llm'
        if llm is None:
            return relevance
        scores = retrieval.llm_scores(llm, model, question,
                                      [c.text for c in chunks])
        final = 0.3 * relevance + 0.7 * scores
        key = 'llm_grade'
    for chunk, score in zip(chunks, scores):
        stage_scores[chunk.id][key] = float(score)
    return final.astype(np.float32)


_ENCODERS: dict[str, retrieval.CrossEncoderReranker] = {}


def _cross_encoder(index):
    from .config import load_lab_settings
    name = load_lab_settings().cross_encoder_model
    if name not in _ENCODERS:
        _ENCODERS[name] = retrieval.CrossEncoderReranker(name)
    return _ENCODERS[name]


def _grade(index, cfg, question, contexts, llm, model):
    """Drop contexts that do not clear the bar, and report whether *nothing*
    did. That second return value is the whole abstention story: a pipeline with
    no gate answers every question, including the ones with no answer."""
    if cfg.grader == 'none':
        return contexts, False
    if cfg.grader == 'lexical':
        grades = [lexical_grade(index, question, c.text) for c in contexts]
    else:
        grades = list(retrieval.llm_scores(llm, model, question,
                                           [c.text for c in contexts])) \
            if llm is not None else [1.0] * len(contexts)
    kept = []
    for context, grade in zip(contexts, grades):
        context.stages['grade'] = float(grade)
        if grade >= cfg.grade_threshold:
            kept.append(context)
    return kept, not kept


def _expand_parents(index, cfg, contexts):
    """Small-to-big: retrieve precisely, then hand the model the surrounding
    text. A message-level hit is often too short to answer from on its own."""
    if cfg.parent_expansion == 'none' or not contexts:
        return contexts
    seen = {c.chunk_id for c in contexts}
    extra: list[Context] = []
    for context in contexts:
        chunk = index.by_id.get(context.chunk_id)
        if chunk is None or not chunk.session_id:
            continue
        if cfg.parent_expansion == 'neighbors':
            candidates = index.neighbors(chunk)
        else:
            candidates = [c for c in index.by_session.get(chunk.session_id, [])
                          if c.layer == chunk.layer]
        for sibling in candidates:
            if sibling.id in seen:
                continue
            seen.add(sibling.id)
            extra.append(Context(chunk_id=sibling.id, text=sibling.text,
                                 layer=sibling.layer, session_id=sibling.session_id,
                                 date=sibling.date, score=context.score * 0.5,
                                 stages={'expanded': 1.0},
                                 expanded_from=context.chunk_id))
    return contexts + extra


def _fit_budget(contexts, max_chars: int):
    """Truncate the *list*, never a chunk: half a diary entry reads as a
    complete one and invites the model to answer from a sentence whose second
    half changed the meaning."""
    out, used = [], 0
    for context in contexts:
        if out and used + len(context.text) > max_chars:
            continue
        out.append(context)
        used += len(context.text)
    return out


ANSWER_PROMPT = (
    'تو دستیار یادداشت‌های روزانه یک کاربر فارسی‌زبانی. فقط بر اساس تکه‌های '
    'دفترچه که در ادامه می‌آید جواب بده. کوتاه و مشخص جواب بده و تاریخ‌ها را '
    'ذکر کن. اگر چیزی عوض شده، وضعیت *آخر* را بگو. بعد از هر ادعا شناسه نشستی '
    f'که از آن برداشتی را داخل [] بگذار. اگر جواب در تکه‌ها نیست، دقیقاً بنویس: '
    f'«{REFUSAL}» و چیزی از خودت نساز. اگر فرض سؤال غلط است، آن را اصلاح کن.')


def answer(outcome: Outcome, cfg: GenerationConfig, llm=None,
           model: str = '') -> Outcome:
    if cfg.answerer == 'none':
        return outcome
    if outcome.abstained or not outcome.contexts:
        outcome.answer = REFUSAL
        return outcome
    started = time.perf_counter()
    if cfg.answerer == 'extractive':
        outcome.answer = _extractive_answer(outcome)
    else:
        outcome.answer = _llm_answer(outcome, llm, model or cfg.model)
    outcome.timings['answer_ms'] = round((time.perf_counter() - started) * 1000, 1)
    if reads_as_refusal(outcome.answer or '', cfg.answerer):
        outcome.abstained = True
    return outcome


def reads_as_refusal(answer: str, answerer: str) -> bool:
    """Did the model decline?

    Only the LLM answerer can decline in its own words, and only in its opening
    sentence — a refusal phrase deep inside a long answer is the model quoting
    the diary, not refusing. The extractive answerer never declines except by
    emitting the canonical string, because everything else it says is copied out
    of the corpus."""
    if answer.strip() == REFUSAL:
        return True
    if answerer != 'llm':
        return False
    opening = ' '.join(textnorm.sentences(answer)[:1])
    return any(marker in opening for marker in REFUSAL_MARKERS)


def _extractive_answer(outcome: Outcome, limit: int = 3) -> str:
    """A deterministic stand-in for generation, so answer-side metrics have
    something to score with no key and no cost. It is quoting, not answering —
    reported as such, never compared against LLM runs as if it were the same
    system."""
    lines = []
    for context in outcome.contexts[:limit]:
        sentences = textnorm.sentences(context.text)
        best = max(sentences, key=len) if sentences else context.text
        tag = f' [{context.session_id or context.chunk_id}]'
        lines.append(best[:300] + tag)
    return ' '.join(lines)


def _llm_answer(outcome: Outcome, llm, model: str) -> str:
    if llm is None:
        return REFUSAL
    blocks = []
    for context in outcome.contexts:
        label = context.session_id or context.chunk_id
        blocks.append(f'[{label} | {context.date} | {context.layer}]\n{context.text}')
    try:
        turn = llm.chat([{'role': 'system', 'content': ANSWER_PROMPT},
                         {'role': 'user', 'content':
                          f"سؤال: {outcome.question}\n\nتکه‌های دفترچه:\n"
                          + '\n\n'.join(blocks)}], model=model)
        return (turn.content or '').strip() or REFUSAL
    except Exception as error:
        outcome.diagnostics['answer_error'] = str(error)[:200]
        return REFUSAL
