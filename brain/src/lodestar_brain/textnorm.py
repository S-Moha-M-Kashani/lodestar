"""Persian text normalisation and tokenisation.

Retrieval's BM25 half, the lexical reranker and the time filter are all built on
this tokeniser, and so is the RAG lab — which imports this module rather than
keeping its own copy, so the configuration the lab measures and the one
production ships cannot drift apart.

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
