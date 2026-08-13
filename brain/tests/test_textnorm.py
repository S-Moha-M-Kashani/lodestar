"""Persian normalisation and tokenising, which BM25, the lexical reranker and
the time filter all build on.

One test per way this can break retrieval, each covering its own edge cases."""
from lodestar_brain import textnorm


# This is a unit test.
def test_two_spellings_of_the_same_word_normalise_alike():
    # Arabic ي/ك, Persian digits and diacritics are rendering differences, not
    # word differences. If they survive, BM25 under-counts silently.
    assert textnorm.normalize('يك') == textnorm.normalize('یک')
    assert '1405' in textnorm.normalize('سال ۱۴۰۵')
    assert textnorm.normalize('كِتاب') == 'کتاب'
    once = textnorm.normalize('مي‌خواستم  ۳ بار')
    assert textnorm.normalize(once) == once      # called twice in places


# This is a unit test.
def test_farsi_text_produces_tokens_and_noise_does_not():
    # The retired hash embedder matched [a-z0-9]+, so Farsi embedded to the zero
    # vector. A tokeniser that returns nothing here is the same failure.
    assert textnorm.tokens('امروز با مهسا دعوام شد')
    assert textnorm.tokens('') == []
    assert textnorm.tokens('a ؟ _') == []       # single letters and punctuation


# This is a unit test.
def test_stopwords_go_but_content_words_stay():
    tokens = textnorm.tokens('که از به مهسا دعوا')
    assert 'مهسا' in tokens and 'دعوا' in tokens
    assert 'که' not in tokens


# This is a unit test.
def test_stopwords_can_be_kept_for_the_time_filter():
    # It matches phrases like «ماه پیش», whose words the stop list removes.
    assert 'که' in textnorm.tokens('که مهسا', drop_stopwords=False)


# This is a unit test.
def test_a_half_spaced_compound_matches_the_spaced_spelling():
    joined = set(textnorm.tokens('می‌خوام برم باشگاه'))
    assert 'میخوام' in joined
    assert set(textnorm.tokens('می خوام برم باشگاه')) <= joined


# This is a unit test.
def test_character_ngrams_share_a_stem_across_affixes():
    # Only the char-hash embedder uses these, but it is a caller.
    assert set(textnorm.char_ngrams('میخواستم')) & set(textnorm.char_ngrams('نمیخواستم'))
    assert textnorm.char_ngrams('اب', 4) == ['اب']   # shorter than the window
    assert textnorm.char_ngrams('') == []


# This is a unit test.
def test_run_on_speech_splits_into_sentences():
    assert len(textnorm.sentences('رفتم سر کار و بعدش مهسا زنگ زد. خسته بودم')) >= 2
    assert len(textnorm.sentences('کجا رفتی؟ هیچ جا')) == 2
    assert textnorm.sentences('   ') == []
