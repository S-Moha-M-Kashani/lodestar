"""Deterministic Persian↔Latin transliteration variants for query expansion.

A user who types "mahsa" means «مهسا», and one who types «مهسا» may have
written "Mahsa" in an English card. Neither BM25 nor a lexical embedder can
bridge scripts, so recall expands every query token into a handful of
plausible spellings on the other side and lets retrieval score them: a
variant that matches nothing costs nothing.

Generation, not translation: Persian leaves short vowels unwritten, so one
Latin spelling maps to several Persian ones and back. Going Latin→Persian
each medial a/e/o is either dropped (short, unwritten) or kept long;
going Persian→Latin a/e/o is optionally inserted between adjacent
consonants. Ambiguous sounds map to their most common letter (s→س not ص/ث)
— a rare spelling missed here is a variant that scores zero, never a wrong
answer promoted.
"""
from itertools import product

# Longest-first: digraphs must win over their single-letter halves.
_LATIN = (('kh', 'خ'), ('gh', 'ق'), ('sh', 'ش'), ('ch', 'چ'), ('zh', 'ژ'),
          ('aa', 'آ'), ('oo', 'و'), ('ou', 'و'), ('ee', 'ی'),
          ('b', 'ب'), ('p', 'پ'), ('t', 'ت'), ('s', 'س'), ('j', 'ج'),
          ('h', 'ه'), ('d', 'د'), ('z', 'ز'), ('r', 'ر'), ('f', 'ف'),
          ('q', 'ق'), ('k', 'ک'), ('g', 'گ'), ('l', 'ل'), ('m', 'م'),
          ('n', 'ن'), ('v', 'و'), ('w', 'و'), ('y', 'ی'), ('c', 'ک'),
          ('x', 'خ'), ('i', 'ی'), ('u', 'و'), ('a', 'ا'), ('e', 'ه'),
          ('o', 'و'))
_SHORT_VOWELS = frozenset('aeo')

_PERSIAN = {'ا': 'a', 'آ': 'a', 'ب': 'b', 'پ': 'p', 'ت': 't', 'ث': 's',
            'ج': 'j', 'چ': 'ch', 'ح': 'h', 'خ': 'kh', 'د': 'd', 'ذ': 'z',
            'ر': 'r', 'ز': 'z', 'ژ': 'zh', 'س': 's', 'ش': 'sh', 'ص': 's',
            'ض': 'z', 'ط': 't', 'ظ': 'z', 'ع': '', 'غ': 'gh', 'ف': 'f',
            'ق': 'gh', 'ک': 'k', 'گ': 'g', 'ل': 'l', 'م': 'm', 'ن': 'n',
            'و': 'u', 'ه': 'h', 'ی': 'i', 'ئ': 'i', 'ء': '', '‌': ''}
_LATIN_VOWELS = frozenset('aeiou')

MAX_VARIANTS = 16
_MIN_LEN = 3   # 'is', 'به': too short to transliterate into anything but noise


def _bounded(choices: list[tuple[str, ...]]) -> list[str]:
    """First MAX_VARIANTS distinct combinations, without materialising the
    product — a long word's ambiguity is exponential and the token comes
    straight from a user query."""
    out: list[str] = []
    for combo in product(*choices):
        word = ''.join(combo)
        if word and word not in out:
            out.append(word)
        if len(out) >= MAX_VARIANTS:
            break
    return out


def _segments_latin(token: str) -> list[str] | None:
    """Cut a Latin token into mapped segments, longest digraph first."""
    out, i = [], 0
    while i < len(token):
        for latin, _ in _LATIN:
            if token.startswith(latin, i):
                out.append(latin)
                i += len(latin)
                break
        else:
            return None   # a character no mapping knows: emit no variant
    return out


def _latin_to_persian(token: str) -> list[str]:
    segments = _segments_latin(token)
    if segments is None:
        return []
    table = dict(_LATIN)
    choices: list[tuple[str, ...]] = []
    for pos, seg in enumerate(segments):
        mapped = table[seg]
        # A medial short vowel is normally unwritten; keep both spellings.
        # Finals stay: «مهسا» ends in a written ا, «خانه» in a written ه.
        if seg in _SHORT_VOWELS and 0 < pos < len(segments) - 1:
            choices.append(('', mapped))
        else:
            choices.append((mapped,))
    return _bounded(choices)


def _persian_to_latin(token: str) -> list[str]:
    mapped = [_PERSIAN.get(ch) for ch in token]
    if None in mapped:
        return []
    parts = [m for m in mapped if m]
    if not parts:
        return []
    # An unwritten short vowel may hide between two consonants: offer each.
    choices: list[tuple[str, ...]] = [(parts[0],)]
    for prev, cur in zip(parts, parts[1:]):
        consonants = (prev[-1] not in _LATIN_VOWELS
                      and cur[0] not in _LATIN_VOWELS)
        choices.append(('', 'a', 'e', 'o') if consonants else ('',))
        choices.append((cur,))
    return _bounded(choices)


def variants(token: str) -> list[str]:
    """Other-script spellings of one token, most literal first. Empty when the
    token is too short, mixed-script, or contains anything unmapped."""
    if len(token) < _MIN_LEN:
        return []
    if all(ch in _PERSIAN for ch in token):
        return _persian_to_latin(token)
    if token.isascii() and token.isalpha():
        return _latin_to_persian(token.lower())
    return []


"""Alternatives considered

**"Why did you write your own transliterator?"**

*Short answer.* Because the task is not transliteration — it is generating
the handful of spellings a *retrieval query* should also try, in both
directions, offline, deterministically. Libraries transliterate one way and
return one answer; one answer is exactly what misses «مهسا» when the user
typed "mahsa".

*Why the obvious option fails.* The obvious option is a single canonical
romanisation (or an LLM call). Persian writes no short vowels, so
«مهسا» romanises to "prya"/"pria" — never the "mahsa" a human types — and
string-equality fails silently. An LLM call would translate well but puts a
model on the hot path of every keystroke-cheap search and is gone when the
brain runs offline.

*Why not the framework.* LangChain has nothing here; this feeds
`expand_queries`, which is already ours. The framework *is* used one layer
up: the variants flow into the same retrievers and RRF fusion as every
other query.

*Libraries that would do it.* `polyglot`/`PyICU` transliteration (ICU:
heavy native dependency, one canonical output); `unidecode` (Latin-only,
lossy, one output); `hazm`/`parsivar` (Persian NLP, no Latin→Persian
generation); a greenfield pick would be ICU plus a fuzzy index
(e.g. RapidFuzz) over romanised shadow tokens.

*Why not adopted.* Decisive: every candidate returns one canonical form,
and the requirement is a small *set* of plausible forms feeding BM25 —
generation with bounded ambiguity, ~40 lines, no new dependency. What would
change the decision: a measured recall gap on a labelled cross-script query
set (the RAG lab can host it); if rule-generated variants miss real user
spellings there, an ICU + fuzzy-matching pass earns its dependency.
"""
