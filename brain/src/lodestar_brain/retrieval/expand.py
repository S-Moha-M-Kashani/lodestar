"""Query understanding: what a question is actually asking for.

One question goes in and several searches come out — the question itself, its
keyword form, a synonym-substituted variant and a cross-script one. All
deterministic, so it can always be on; `multi_query` is the model-backed
alternative for when a fixed table is not enough.
"""
from langchain_classic.retrievers import MultiQueryRetriever
from langchain_core.retrievers import BaseRetriever

from .. import textnorm, translit

# Paraphrases a board genuinely alternates between, for deterministic query
# expansion: cheap recall for the lexical half, which otherwise misses «همسرم»
# on a board that only ever writes «مهسا».
SYNONYMS = {
    'همسرم': ('مهسا',), 'زنم': ('مهسا',), 'مهسا': ('همسرم',),
    'مادرم': ('مامان',), 'مامان': ('مادرم',), 'پدرم': ('بابا',),
    'شغل': ('کار', 'جاب'), 'کار': ('شغل',), 'استخدام': ('آفر', 'قبول'),
    'بحث': ('دعوا',), 'دعوا': ('بحث', 'قهر'),
    'مالیات': ('اداره مالیات', 'جریمه'), 'ورزش': ('باشگاه',),
    'اپلای': ('درخواست', 'رزومه'), 'ریجکت': ('جواب رد', 'قبول نشدم'),
    'خونه': ('آپارتمان', 'اجاره'), 'خواب': ('بیخوابی', 'بی خوابی'),
}
# Interrogatives only. Ordinary English stopwords are deliberately absent: this
# set strips the *asking* from a question so the lexical half scores content
# words, and dropping 'the' from "renew the visa" would change the phrase a
# BM25 query is trying to match.
QUESTION_WORDS = frozenset("""
چی چه چرا چطور چگونه کجا کِی کی چند چقدر آیا بگو بهم راجب درباره درمورد هست بود
شد کردم دادم گفتم میشه کدوم کدام حالم وضعیت
what when where why how who whom which did does was were tell about
""".split())


def keyword_query(question: str) -> str:
    """Strip the asking, keep the subject, so lexical retrieval scores content
    words rather than 'how' and 'what'."""
    kept = [token for token in textnorm.tokens(question)
            if token not in QUESTION_WORDS]
    return ' '.join(kept) or question


def expand_queries(question: str) -> list[str]:
    """Deterministic multi-query expansion: the question, its keyword form, a
    synonym-substituted variant, and a cross-script variant ("mahsa" also
    searches «مهسا» and back). No model, so it can always be on — and the
    question itself always leads, so nothing is retrieved *instead* of it."""
    variants = [question]
    keywords = keyword_query(question)
    if keywords != question:
        variants.append(keywords)
    swapped: list[str] = []
    for token in textnorm.tokens(question):
        swapped.extend(SYNONYMS.get(token, ()))
    if swapped:
        variants.append(f"{keywords} {' '.join(dict.fromkeys(swapped))}")
    crossed: list[str] = []
    for token in textnorm.tokens(question):
        crossed.extend(translit.variants(token))
    if crossed:
        variants.append(f"{keywords} {' '.join(dict.fromkeys(crossed))}")
    return list(dict.fromkeys(variants))


def multi_query(base: BaseRetriever, llm) -> BaseRetriever:
    """The model-backed alternative to `expand_queries`, taken from LangChain
    rather than reimplemented. It writes the paraphrases a fixed synonym table
    cannot know, at one LLM call per question."""
    return MultiQueryRetriever.from_llm(retriever=base, llm=llm)
