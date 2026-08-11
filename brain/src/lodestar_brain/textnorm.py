"""Persian text normalisation and tokenisation.

Retrieval's BM25 half, the lexical reranker and the time filter are all built on
this tokeniser. The RAG lab used to import it for the same reason, so the
configuration it measured and the one production ships could not drift apart; the
lab moved to its own repository on 2026-08-11 and took a copy, so that guarantee
is gone. A change here is a change to what this project retrieves with, and the
lab will not notice it.

The text is colloquial Farsi typed by a human: Arabic ي/ك mixed with Persian
ی/ک, half-spaces present or missing («می خوام» vs «می‌خوام»), Persian and ASCII
digits, stray diacritics. Two texts that a reader would call identical must
tokenise identically or BM25 and every lexical score silently under-counts.

Nothing here is Farsi-only: Latin words ("apply", "CV") survive untouched, which
matters because the user code-switches constantly.
"""
import re
import unicodedata

ZWNJ = '‌'  # نیم‌فاصله — a joiner, not a word boundary

# Arabic letterforms → their Persian equivalents. Same sound, different
# codepoint; a search for «می‌خوای» must match a session that used «ي».
_LETTER_FOLD = str.maketrans({
    'ي': 'ی', 'ك': 'ک', 'ﻯ': 'ی', 'ﻱ': 'ی', 'ٱ': 'ا', 'أ': 'ا', 'إ': 'ا',
    'آ': 'ا', 'ؤ': 'و', 'ئ': 'ی', 'ة': 'ه', 'ۀ': 'ه', 'ء': '',
})
# Harakat (fatha, damma, sukun, tanwin, ...) plus tatweel: decoration only.
_MARKS = re.compile('[ً-ْٓ-ٰٕـ]')
# Persian ۰-۹ and Arabic-Indic ٠-٩ → ASCII, so «۱۴۰۵» and "1405" are one token.
_DIGIT_FOLD = str.maketrans(
    {chr(0x06F0 + i): str(i) for i in range(10)} |
    {chr(0x0660 + i): str(i) for i in range(10)})
_WORD = re.compile(r'[^\W_]+(?:‌[^\W_]+)*', re.UNICODE)

# Persian function words. Kept deliberately short: an aggressive stop list eats
# the negations and pronouns that carry meaning in a diary («نمیدونم», «خودم»),
# so only words with no discriminating power at all are here.
STOPWORDS = frozenset("""
که را از به با در و یا هم این اون آن یه یک بر بی تا هر چه چی می نمی است هست بود
بودم بوده شد شده می‌شه میشه کرد کردم کرده برای روی مثل ولی اما پس یعنی البته
دیگه دیگر خیلی خب حالا الان هنوز باید شاید انقدر اینقدر همه چون تو من ما شما اونا
""".split())


def normalize(text: str) -> str:
    """Fold everything that is a rendering difference rather than a word
    difference. Idempotent: normalize(normalize(x)) == normalize(x)."""
    text = unicodedata.normalize('NFKC', text)
    text = text.translate(_LETTER_FOLD).translate(_DIGIT_FOLD)
    text = _MARKS.sub('', text)
    return re.sub(r'[ \t ]+', ' ', text).strip()


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    """Word tokens for BM25 and lexical scoring.

    Half-spaced compounds are emitted whole *and* split, because the same word
    appears both ways in the corpus: «می‌خوام» must match «می خوام», whose two
    halves tokenise separately."""
    out: list[str] = []
    for match in _WORD.findall(normalize(text).lower()):
        parts = match.split(ZWNJ)
        candidates = [match.replace(ZWNJ, '')] + (parts if len(parts) > 1 else [])
        for token in candidates:
            if len(token) < 2:
                continue
            if drop_stopwords and token in STOPWORDS:
                continue
            out.append(token)
    return out


def char_ngrams(text: str, n: int = 4) -> list[str]:
    """Character n-grams over normalised text. Persian is heavily affixed
    (میخواستم / نمیخواستم / بخوام share a stem no whitespace tokeniser finds),
    so n-grams recover morphological overlap that word tokens miss."""
    flat = re.sub(r'\s+', ' ', normalize(text).lower().replace(ZWNJ, ''))
    if len(flat) <= n:
        return [flat] if flat else []
    return [flat[i:i + n] for i in range(len(flat) - n + 1)]


def sentences(text: str) -> list[str]:
    """Split into sentence-ish units. Spoken-diary text is run-on, so «و بعدش»
    and «خلاصه» are treated as boundaries alongside . ! ? ؟ and newlines —
    without them a whole voice session is one 900-character 'sentence' and
    extractive summarising has nothing to choose between."""
    text = normalize(text)
    parts = re.split(r'(?<=[.!?؟…])\s+|\n+|(?:\s+(?:و بعدش|خلاصه|راستی)\s+)', text)
    return [p.strip() for p in parts if p and p.strip()]


"""Alternatives considered — "why did you write your own tokeniser?"

**Short answer.** Because no framework owns this job, and the one library that
does would have invalidated 31 measured experiments. Normalisation here serves
only the *lexical* retrieval path — BM25, the reranker's IDF term coverage, the
time filter's phrase matching. The dense path never calls it: a
sentence-transformers model tokenises internally with its own subword
vocabulary.

**Why the obvious option fails.** The naive tokeniser is `text.split()`, and it
is wrong for Persian in a way that is silent. «می‌خوام» (zero-width non-joiner)
and «می خوام» (space) are the same word typed two ways; split on whitespace and
they become different BM25 terms, so a query matches half the documents it
should and no error is raised. The same holds for Arabic ي/ك against Persian
ی/ک, and for Persian ۱۲۳ against ASCII 123.

**Why not Unicode normalisation alone.** `unicodedata.normalize('NFKC', ...)`
is used here, but it is not sufficient and it is worth knowing why in an
interview: U+064A ARABIC YEH and U+06CC FARSI YEH are *distinct letters*, not
compatibility variants, so NFKC leaves them apart. Folding them is a
search-time decision — deliberately lossy, correct for retrieval, wrong for
display. `_LETTER_FOLD` is that decision written down.

**Why not LangChain.** It has no normaliser or tokeniser at all. The proof is
in its own `BM25Retriever`, which exposes a `preprocess_func` parameter with a
non-language-aware default: the framework's position is that tokenising is the
caller's job. Chunking (`RecursiveCharacterTextSplitter`), fusion
(`EnsembleRetriever`) and query expansion (`MultiQueryRetriever`) *are* taken
from LangChain in this codebase. This is not one of those seams.

**The libraries that would do it.**

- `hazm` — the standard Persian NLP toolkit: `Normalizer`, `word_tokenize`,
  `Lemmatizer`, POS tagging, and a curated stopword list. The right answer for
  a greenfield project. Pulls `nltk` and a model directory.
- `parsivar` — comparable scope, less actively maintained.
- `spaCy`'s `fa` language class — a tokeniser and stopwords, but no trained
  Persian pipeline, so it buys little over this module.
- Hugging Face `tokenizers` — subword, which is the wrong *unit* for BM25:
  IDF over word-pieces changes what "a term" means and makes rare-literal
  recall worse, which is the one thing BM25 is here for.

**Why they were not adopted.** Two reasons, and the first is decisive.

1. Every BM25 score, IDF weight and lexical rerank in the lab's 31 runs was
   produced by this tokeniser. Replacing it changes those numbers, so old rows
   stop being comparable to new ones — the failure this project guards hardest
   against. A tokeniser swap is an experiment to *measure*, not a refactor.
2. `tokens()` emits a half-spaced compound both joined and split, on purpose,
   because the corpus contains both spellings. A conventional normaliser picks
   one and the other stops matching. That is a recall choice specific to this
   text, not a bug to standardise away.

**What would change the decision.** A lab run with the tokeniser as the only
varied knob, scored on recall and quote recall over
`fixtures/diary_year_fa.json`. If `hazm`'s lemmatiser beats character 4-grams
on morphological overlap, adopt it — and re-run the baseline so the leaderboard
stays internally comparable rather than mixing two tokenisers.
"""
