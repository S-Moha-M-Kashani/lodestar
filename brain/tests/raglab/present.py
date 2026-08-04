"""Read-only presentation helpers shared by the RAG Lab service (:9002) and its
read-only Inspector (:9003).

These used to live in `inspector.py` alone. The lab now records what a job
actually ran and needs to describe it the same two ways the Inspector already
does — chunks grouped by session after indexing, and which retrieval
candidates are gold — so `server.py` needs them too. `inspector.py` already
imports `Jobs` from `server.py`; `server.py` importing back from `inspector.py`
would be a circular import, so both import from this module instead.
"""
from lodestar_brain import textnorm


def _norm(text: str) -> str:
    return ' '.join(textnorm.tokens(text, drop_stopwords=False))


def mark_gold(candidate_texts: list[str],
              evidence_quotes: list[str]) -> list[bool]:
    """Which candidates contain a question's gold evidence quote.

    Substring either direction over the shared normaliser: a chunk may be
    smaller than a quote (part of one message) or larger (several). Normalising
    first means a whitespace or zero-width difference cannot hide a real match —
    the same reason the tokeniser is shared across the whole brain. A candidate
    that normalises to the empty string (blank, whitespace-only, or nothing the
    tokeniser keeps) is never gold — the empty string is a substring of every
    quote, which would mark a chunk with no evidence at all as a match. The same
    guard applies to a quote: one that normalises to nothing (punctuation-only,
    or a single short token the tokeniser drops) is dropped from the quote list
    rather than matching every candidate for the same reason."""
    return _gold_flags([_norm(text) for text in candidate_texts], evidence_quotes)


def _gold_flags(normalised_texts: list[str],
                evidence_quotes: list[str]) -> list[bool]:
    """The marking itself, over text that is already normalised — so a caller
    counting gold across many questions normalises the corpus once instead of
    once per question."""
    quotes = [n for n in (_norm(q) for q in evidence_quotes) if n]
    return [bool(text) and any(q in text or text in q for q in quotes)
            for text in normalised_texts]


def normalised_chunks(index) -> list[str]:
    """Every chunk in the index, normalised once, for `gold_available`."""
    return [_norm(chunk.text) for chunk in index.chunks]


def gold_available(index, evidence_quotes: list[str],
                   norm_chunks: list[str] | None = None) -> int:
    """How many chunks in the whole index hold this question's evidence.

    The denominator that turns "1 gold" into a result: 1 of how many there were
    to find. Counted over chunks rather than over the fixture's list of evidence
    quotes, because the two are not the same number — one quote can be split
    across two chunks by a length-based chunker, and one chunk can carry two
    quotes. What a reader needs to know is what retrieval *could* have found at
    this chunk size, which changes with the chunker and so cannot be read off
    the ground truth alone."""
    norms = norm_chunks if norm_chunks is not None else normalised_chunks(index)
    return sum(_gold_flags(norms, evidence_quotes))


def evidence_spans(text: str, evidence_quotes: list[str]) -> list[list[int]]:
    """Where a question's gold evidence sits inside one candidate's text, as
    `[start, end]` character ranges in reading order.

    This is what the Inspector paints green, which is why it is computed here
    and not in the browser. `mark_gold` calls a candidate gold when the quote
    contains it **as well as** when it contains the quote — a chunk smaller than
    its quote is real evidence and must still be marked — but that candidate
    holds no verbatim quote to highlight. Guessing a range for it would draw a
    green stripe over text the ground truth never quoted, so those return
    nothing and the row is gold with no highlight, which is the truth.

    Overlapping and touching ranges are merged: two `<mark>` elements over the
    same characters nest, and nested marks render as a darker stripe that reads
    as a third kind of evidence. Verbatim `str.find` rather than the normaliser
    the matching uses, because a range only means something against the exact
    characters the page will render."""
    found: list[list[int]] = []
    for quote in evidence_quotes:
        if not quote:
            continue
        start = text.find(quote)
        while start != -1:
            found.append([start, start + len(quote)])
            start = text.find(quote, start + 1)
    merged: list[list[int]] = []
    for start, end in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def chunks_by_session(index) -> list[dict]:
    """Every chunk the index holds, grouped by session in index order — the
    'chunks after indexing' view. `by_session` is built in chunk order, which
    follows diary order, so no sorting is needed or wanted."""
    groups = []
    for session_id, chunks in index.by_session.items():
        groups.append({
            'session_id': session_id,
            'date': chunks[0].date if chunks else '',
            'chunks': [{'id': c.id, 'text': c.text} for c in chunks]})
    return groups
