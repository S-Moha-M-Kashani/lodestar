"""Embedders for the lab, behind the production `Embedder` protocol.

`ascii-hash` is the embedder the brain ships with today. It is here as a
*baseline*, and it is expected to score near zero on this corpus: its tokeniser
is `[a-z0-9]+`, so a Farsi sentence contains no tokens at all and every vector
is the zero vector. That is the whole point of measuring it — the lab should
show, in numbers, that shipping diary memory on the current default would
retrieve nothing.

`token-hash` and `char-hash` are offline, dependency-free, and Unicode-aware, so
CI can measure real retrieval without downloading a model. `fastembed` is the
honest ceiling: a multilingual transformer (Persian is in its training mix,
unlike bge-small-en, which the brain hardwires today).

**Which languages an option covers is the first fact about it**, so every entry
below carries that as text the panel shows next to the dropdown. On a Farsi diary
this is not a footnote: most of the famous embedders — bge-small-en,
all-MiniLM-L6 — are English-only models that would produce a full set of
plausible-looking numbers measuring nothing at all. The same rule as the chat
models applies to the model list: an option nobody has run here stays listed as
NA rather than being dropped, and availability is *verified* against what
fastembed actually serves instead of guessed.

**Queries and passages are embedded separately** because the E5 family was
trained that way ("query: " / "passage: "). Omitting the prefixes is a silent
accuracy loss, which is exactly the kind of loss this lab exists to prevent, so
the prefixes belong to the model entry and retrieval goes through
`query_vectors()` rather than calling `embed()` on a question.
"""
import hashlib
from dataclasses import dataclass

import numpy as np

from lodestar_brain.rag.embedder import HashEmbedder, _normalize

from . import textnorm

TOKEN_DIM = 512
CHAR_DIM = 1024
DEFAULT_FASTEMBED = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
DEFAULT_LOCAL = 'heydariAI/persian-embeddings'
DEFAULT_API = 'openai/text-embedding-3-small'

# Which kind serves a model, and which model that kind loads when nothing is
# pinned — the same rule as '' meaning RAGLAB_MODEL for the chat roles.
BACKENDS = ('fastembed', 'sentence-transformers', 'openai')
BACKEND_DEFAULTS = {
    'fastembed': DEFAULT_FASTEMBED,
    'sentence-transformers': DEFAULT_LOCAL,
    'openai': DEFAULT_API,
}

# Qwen3's embedding models are instruction-tuned: the query side carries a task
# instruction and the document side is left plain. Same shape as the E5 prefixes,
# same reason for keeping it beside the model entry.
QWEN3_INSTRUCT = ('Instruct: Given a question about a personal diary, retrieve '
                  'the diary passages that answer it\nQuery: ')

# The vocabulary the panel says language coverage in. Kept as constants because
# the words matter: "any script" means the embedder is not *blind* to Farsi, not
# that it understands it — both hash embedders are lexical.
LATIN_ONLY = 'Latin script only (a–z, 0–9)'
ANY_SCRIPT = 'any script, lexical only (no meaning)'
ENGLISH_ONLY = 'English only'
MULTI_50 = 'English + Farsi (50+ languages)'
MULTI_100 = 'English + Farsi (100+ languages)'
BY_MODEL = 'English + Farsi — depends on the model below'
FARSI_TUNED = 'Farsi + English (Persian-tuned)'


def _bucket(token: str, dim: int) -> int:
    return int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16) % dim


class TokenHashEmbedder:
    """Hashed bag of normalised Persian/Latin words with sub-linear term
    frequency. Lexical, but at least it sees the language."""
    dim = TOKEN_DIM
    name = 'token-hash'

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            counts: dict[int, float] = {}
            for token in textnorm.tokens(text):
                slot = _bucket(token, self.dim)
                counts[slot] = counts.get(slot, 0.0) + 1.0
            for slot, count in counts.items():
                out[i, slot] = 1.0 + np.log(count)   # damp repetition
        return _normalize(out)


class CharHashEmbedder:
    """Hashed character 4-grams. Persian inflects by affix (میخواستم /
    نمیخوام / بخوام), which whitespace tokens treat as three unrelated words;
    n-grams recover the shared stem, so this is the strongest offline option."""
    dim = CHAR_DIM
    name = 'char-hash'

    def __init__(self, n: int = 4):
        self.n = n

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for gram in textnorm.char_ngrams(text, self.n):
                out[i, _bucket(gram, self.dim)] += 1.0
        return _normalize(out)


class FastEmbedMultilingual:
    """Real sentence embeddings. Wraps fastembed directly rather than the
    brain's FastEmbedEmbedder default, because that default is bge-small-**en**
    — an English-only model scored against a Farsi corpus.

    `factory` exists so the prefix behaviour is testable without downloading a
    model; production leaves it alone."""
    name = 'fastembed'

    def __init__(self, model_name: str, batch_size: int = 64,
                 query_prefix: str = '', passage_prefix: str = '', factory=None):
        self.model = (factory or _text_embedding)(model_name)
        self.model_name = model_name
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        # The model is part of the identity: two different transformers are two
        # different representations, and anything caching by embedder name has to
        # be able to tell them apart.
        self.name = f'fastembed:{model_name}'
        self.dim = len(next(iter(self.model.embed(['probe']))))

    def _vectors(self, texts: list[str], prefix: str) -> np.ndarray:
        payload = [prefix + text for text in texts] if prefix else list(texts)
        vectors = np.array(list(self.model.embed(payload,
                                                 batch_size=self.batch_size)),
                           dtype=np.float32)
        return _normalize(vectors)

    def embed(self, texts: list[str]) -> np.ndarray:
        """The document side. Everything stored in Chroma comes through here."""
        return self._vectors(list(texts), self.passage_prefix)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        """The query side. Same model, the prefix it was trained to expect."""
        return self._vectors(list(texts), self.query_prefix)


def _text_embedding(model_name: str):
    from fastembed import TextEmbedding  # optional 'semantic' extra
    return TextEmbedding(model_name)


def _sentence_transformer(model_name: str):
    from sentence_transformers import SentenceTransformer  # 'local-embeddings'
    return SentenceTransformer(model_name)


class SentenceTransformerEmbedder:
    """Any HuggingFace checkpoint, through sentence-transformers.

    This is the backend that reaches the models fastembed does not serve — the
    Persian-tuned encoders and Qwen3 — which on a Farsi diary are the ones worth
    measuring. The model's own `modules.json` decides pooling, so a checkpoint
    that ships an ST configuration is used the way its authors intended rather
    than mean-pooled by us and quietly mis-scored.

    `factory` exists so the prefix behaviour is testable without a 2 GB download;
    production leaves it alone.
    """
    name = 'sentence-transformers'

    def __init__(self, model_name: str, batch_size: int = 32,
                 query_prefix: str = '', passage_prefix: str = '', factory=None):
        self.model = (factory or _sentence_transformer)(model_name)
        self.model_name = model_name
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        # Two different checkpoints are two different representations, so the
        # model is part of the identity anything caching by name sees.
        self.name = f'st:{model_name}'
        # Renamed in sentence-transformers 5; the lab floor is 3, so ask for the
        # new name and fall back rather than emitting a deprecation warning on
        # every build (or breaking on the version that drops the old one).
        dimension = (getattr(self.model, 'get_embedding_dimension', None)
                     or self.model.get_sentence_embedding_dimension)
        self.dim = int(dimension())

    def _vectors(self, texts: list[str], prefix: str) -> np.ndarray:
        payload = [prefix + text for text in texts] if prefix else list(texts)
        vectors = self.model.encode(payload, batch_size=self.batch_size,
                                    show_progress_bar=False,
                                    convert_to_numpy=True)
        # Normalised here rather than trusting the flag: cosine similarity in
        # Chroma is only cosine similarity if the vectors are unit length.
        return _normalize(np.asarray(vectors, dtype=np.float32))

    def embed(self, texts: list[str]) -> np.ndarray:
        return self._vectors(list(texts), self.passage_prefix)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._vectors(list(texts), self.query_prefix)


class OpenAIEmbedder:
    """OpenAI's embedding API: no download, but a key and a bill.

    Deliberately not routed through OpenRouter — it serves chat completions only,
    so the lab's LLM key cannot pay for this. The dimension is *declared* from the
    catalogue rather than probed, because building an index configuration must not
    cost an API call.

    `post` exists so the wire format is testable offline; production leaves it
    alone.
    """
    name = 'openai'

    def __init__(self, model_name: str, settings, batch_size: int = 256,
                 post=None):
        key = getattr(settings, 'openai_api_key', '') or ''
        if not key:
            raise ValueError(
                f'{model_name} needs an OpenAI key: set OPENAI_API_KEY (the '
                'OpenRouter key cannot be used — OpenRouter serves no '
                'embeddings endpoint)')
        self.model_name = model_name
        # The panel shows OpenRouter-shaped slugs so every model list reads the
        # same way; OpenAI's own API wants the bare name.
        self.wire_name = model_name.split('/', 1)[-1]
        self.key = key
        self.base_url = (getattr(settings, 'openai_base_url', '')
                         or 'https://api.openai.com/v1').rstrip('/')
        self.batch_size = batch_size
        self._post = post or _post_json
        self.name = f'openai:{model_name}'
        entry = _MODELS.get(model_name)
        self.dim = entry.dim if entry else 0

    def embed(self, texts: list[str]) -> np.ndarray:
        texts = list(texts)
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            body = self._post(f'{self.base_url}/embeddings',
                              {'model': self.wire_name, 'input': batch},
                              {'Authorization': f'Bearer {self.key}',
                               'Content-Type': 'application/json'})
            out.extend(item['embedding'] for item in body['data'])
        return _normalize(np.asarray(out, dtype=np.float32))


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    import httpx
    res = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    res.raise_for_status()
    return res.json()


def query_vectors(embedder, texts: list[str]) -> np.ndarray:
    """Embed questions as questions when the model distinguishes them.

    Asked through a helper rather than a method on the protocol so every hash
    embedder — and the brain's own — keeps working untouched: an embedder with no
    query side is simply symmetric."""
    asymmetric = getattr(embedder, 'embed_queries', None)
    if callable(asymmetric):
        return asymmetric(list(texts))
    return embedder.embed(list(texts))


# --- what each option can actually read ------------------------------------

@dataclass(frozen=True)
class EmbedderHint:
    """One line of truth about an embedder kind, shown next to the dropdown."""
    kind: str
    label: str
    languages: str
    farsi: bool          # can it represent this corpus at all?
    note: str

    def as_dict(self, available: bool) -> dict:
        return {'kind': self.kind, 'label': self.label,
                'languages': self.languages, 'farsi': self.farsi,
                'note': self.note, 'available': available}


EMBEDDER_HINTS = (
    EmbedderHint('ascii-hash', 'ascii-hash — the brain default today',
                 LATIN_ONLY, False,
                 'tokenises [a-z0-9]+, so a Farsi sentence has no tokens and '
                 'every vector is zero: measured 0.014 session recall here, i.e. '
                 'chance. Kept as the baseline that makes the point.'),
    EmbedderHint('token-hash', 'token-hash — hashed Persian words', ANY_SCRIPT,
                 True,
                 'Unicode-aware bag of normalised words. Sees the language but '
                 'not the meaning, and misses «نمیخوام» against «میخواستم».'),
    EmbedderHint('char-hash', 'char-hash — hashed character 4-grams', ANY_SCRIPT,
                 True,
                 'Recovers Persian stems across affixes, which is why it is the '
                 'strongest option that downloads nothing: 0.386 recall against '
                 'ascii-hash\'s 0.014. Still lexical — a paraphrase with no '
                 'shared letters is invisible to it.'),
    EmbedderHint('fastembed', 'fastembed — a real transformer', BY_MODEL, True,
                 'Embeds meaning rather than letters (0.612 recall measured), '
                 'from its own short ONNX list. Which languages it covers is '
                 'decided by the model below — pick a multilingual one or this '
                 'becomes an English model reading a Farsi diary.'),
    EmbedderHint('sentence-transformers',
                 'sentence-transformers — any HuggingFace model', BY_MODEL, True,
                 'The lab default, because it is the only backend that can reach '
                 'a Persian-tuned encoder: fastembed serves neither '
                 'heydariAI/persian-embeddings nor Qwen3. Needs the '
                 'local-embeddings extra and downloads the weights once.'),
    EmbedderHint('openai', 'openai — the embeddings API', BY_MODEL, True,
                 'No download and no local compute, but it needs OPENAI_API_KEY '
                 'and spends money on every build. Deliberately not the '
                 'OpenRouter key: OpenRouter serves no embeddings endpoint.'),
)

_HINTS = {hint.kind: hint for hint in EMBEDDER_HINTS}


@dataclass(frozen=True)
class EmbedModel:
    """A concrete embedding model, and the backend that can load it.

    `backend` is not decoration: it decides what picking this model costs (an ONNX
    download, a 2 GB checkpoint, or an API bill) and it is what makes availability
    checkable — one probe per backend instead of one list for all of them."""
    id: str
    label: str
    languages: str
    farsi: bool
    source: str          # open | closed | unknown
    dim: int
    note: str
    backend: str = 'fastembed'
    # A one-word standing shown in the option itself. The reason it is not left to
    # the explainer: which model to reach for is the question being asked *while*
    # the dropdown is open, and nobody opens an explainer to find out.
    tag: str = ''
    query_prefix: str = ''
    passage_prefix: str = ''

    def as_dict(self, available: bool) -> dict:
        return {'id': self.id, 'label': self.label, 'languages': self.languages,
                'farsi': self.farsi, 'source': self.source, 'dim': self.dim,
                'note': self.note, 'available': available,
                'backend': self.backend, 'tag': self.tag,
                'query_prefix': self.query_prefix,
                'passage_prefix': self.passage_prefix}


# Deliberately short, and deliberately including the two English-only models: the
# lab has to be able to *measure* the choice the brain ships with, and a reader
# comparing rows needs to see why that row scored nothing.
EMBED_MODELS = (
    # The lab default. Persian-tuned rather than Persian-capable: an XLM-RoBERTa
    # fine-tuned on Persian, which is a different bet from a multilingual model
    # that merely includes Farsi. Its own card recommends sentence-transformers
    # first, and ships modules.json + 1_Pooling, so ST applies the mean pooling
    # the authors trained with instead of us guessing one. 1024 dims and a
    # 514-position encoder, so the session/month/thread rollups get truncated —
    # worth remembering when reading a rollup score.
    EmbedModel(DEFAULT_LOCAL, 'persian-embeddings (heydariAI)', FARSI_TUNED,
               True, 'open', 1024,
               'the lab default: fine-tuned on Persian specifically rather than '
               'multilingual-by-accident, ~2.2 GB, and the cheapest real encoder '
               'to try on this corpus. Loaded through sentence-transformers, '
               'which is what its model card recommends. ~512-token context.',
               backend='sentence-transformers', tag='lab default'),
    EmbedModel('Qwen/Qwen3-Embedding-8B', 'Qwen3-Embedding-8B', MULTI_100, True,
               'open', 4096,
               'the recommended ceiling: top of the public multilingual '
               'retrieval boards, 40k context, instruction-tuned (the lab sends '
               'the instruction on the query side for you). ~16 GB and slow on a '
               'laptop, so measure the default first.',
               backend='sentence-transformers', tag='recommended',
               query_prefix=QWEN3_INSTRUCT),
    EmbedModel(DEFAULT_API, 'text-embedding-3-small', MULTI_100, True, 'closed',
               1536,
               'no download and no local compute: the cheapest way to get a '
               'strong multilingual encoder onto this corpus, at the cost of '
               'sending the diary to an API. Needs OPENAI_API_KEY.',
               backend='openai'),
    EmbedModel(DEFAULT_FASTEMBED, 'paraphrase-multilingual-MiniLM-L12-v2',
               MULTI_50, True, 'open', 384,
               'the lab default: smallest multilingual option, ~120 MB, 384 dims. '
               'Fine as a floor, and the weakest of the Farsi-capable models.'),
    EmbedModel('sentence-transformers/paraphrase-multilingual-mpnet-base-v2',
               'paraphrase-multilingual-mpnet-base-v2', MULTI_50, True, 'open',
               768,
               'twice the width of the default over the same languages — the '
               'cheapest A/B worth running first.'),
    EmbedModel('intfloat/multilingual-e5-large', 'multilingual-e5-large',
               MULTI_100, True, 'open', 1024,
               'the strongest widely served multilingual retriever; trained for '
               'retrieval specifically, not paraphrase similarity. ~2 GB on first '
               'use, and it needs its query/passage prefixes to perform — the lab '
               'applies them for you.',
               query_prefix='query: ', passage_prefix='passage: '),
    EmbedModel('intfloat/multilingual-e5-small', 'multilingual-e5-small',
               MULTI_100, True, 'open', 384,
               'a fifth of e5-large\'s size with the same recipe — worth '
               'measuring before paying for the big one.',
               query_prefix='query: ', passage_prefix='passage: '),
    EmbedModel('BAAI/bge-m3', 'bge-m3', MULTI_100, True, 'open', 1024,
               'the best-regarded multilingual retriever on Persian in public '
               'evals. fastembed serves it for sparse/hybrid search and dense '
               'support varies by version, so NA here means "check your '
               'fastembed", not "not worth it".'),
    EmbedModel('jinaai/jina-embeddings-v3', 'jina-embeddings-v3', MULTI_100,
               True, 'open', 1024,
               'task-specific adapters and 8k context, so a whole diary session '
               'fits in one vector without being cut.'),
    EmbedModel('openai/text-embedding-3-large', 'text-embedding-3-large',
               MULTI_100, True, 'closed', 3072,
               'the strongest API option and the widest vector here (3072 dims). '
               'Same trade as its small sibling — a key, a bill, and the diary '
               'leaving the machine — for a few points more.',
               backend='openai'),
    EmbedModel('BAAI/bge-small-en-v1.5', 'bge-small-en-v1.5', ENGLISH_ONLY,
               False, 'open', 384,
               'what the brain hardwires today. Here as the baseline: it will '
               'return confident numbers that mean nothing on Farsi text.'),
    EmbedModel('sentence-transformers/all-MiniLM-L6-v2', 'all-MiniLM-L6-v2',
               ENGLISH_ONLY, False, 'open', 384,
               'the most-copied embedder on the internet, and the wrong one for '
               'this corpus.'),
)

MODEL_IDS = tuple(model.id for model in EMBED_MODELS)
_MODELS = {model.id: model for model in EMBED_MODELS}


def fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401
    except Exception:
        return False
    return True


def fastembed_models() -> frozenset:
    """Model ids this installation of fastembed can actually serve.

    Verified, never guessed: with fastembed missing (or its API changed) the
    answer is the empty set, which shows every model as NA rather than promising
    a download that will fail halfway through a sweep."""
    try:
        from fastembed import TextEmbedding
        return frozenset(entry['model']
                         for entry in TextEmbedding.list_supported_models()
                         if entry.get('model'))
    except Exception:
        return frozenset()


def sentence_transformers_available() -> bool:
    """Whether the local-embeddings extra is installed. Import-checked, not
    guessed: without it every HuggingFace checkpoint is NA."""
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def openai_embeddings_available(settings=None) -> bool:
    """A key is the whole requirement — there is nothing to install. Checked
    because a missing key would otherwise fail on the first chunk of a build."""
    return bool(getattr(settings, 'openai_api_key', '') or '')


def backend_availability(settings=None) -> dict:
    """Which of the three model backends can be used right now."""
    return {'fastembed': fastembed_available(),
            'sentence-transformers': sentence_transformers_available(),
            'openai': openai_embeddings_available(settings)}


def embedder_hints(settings=None) -> list[dict]:
    """One hint per embedder kind, in registry order. The hash embedders are
    always available; the three model backends answer for themselves, so a kind
    that cannot run says NA instead of failing when a run starts."""
    live = backend_availability(settings)
    return [hint.as_dict(live.get(hint.kind, True)) for hint in EMBEDDER_HINTS]


def embed_model_catalogue(settings=None) -> list[dict]:
    """The embedding-model dropdown: the lab default first, then every candidate
    with its language coverage, licence, backend, and whether it can be loaded
    now. Availability is per backend — a fastembed list cannot answer for a
    HuggingFace checkpoint or for an API key."""
    default_id = getattr(settings, 'fastembed_model', None) or DEFAULT_FASTEMBED
    served = fastembed_models()
    live = backend_availability(settings)
    known = list(EMBED_MODELS)
    if default_id not in _MODELS:
        # An id set by RAGLAB_FASTEMBED_MODEL is by definition one the user wants.
        known.insert(0, EmbedModel(default_id, default_id,
                                   'coverage not recorded — check the model card',
                                   False, 'unknown', 0,
                                   'named by RAGLAB_FASTEMBED_MODEL'))

    def usable(model: EmbedModel) -> bool:
        if not live.get(model.backend):
            return False
        # fastembed is the one backend that also publishes *which* models it
        # serves, so that list is honoured on top of the import check.
        return model.id in served if model.backend == 'fastembed' else True

    entries = [model.as_dict(usable(model)) for model in known]
    entries.sort(key=lambda entry: not entry['available'])
    # '' pins nothing: each backend loads its own default, so switching backend
    # switches model without a second edit.
    return [{'id': '', 'label': 'the backend\'s own default',
             'languages': BY_MODEL, 'farsi': True, 'source': 'default',
             'dim': 0, 'available': True, 'backend': '', 'tag': '',
             'note': 'sentence-transformers → '
                     f'{_short(DEFAULT_LOCAL)}; fastembed → '
                     f'{_short(default_id)} (RAGLAB_FASTEMBED_MODEL); openai → '
                     f'{_short(DEFAULT_API)}',
             'query_prefix': '', 'passage_prefix': ''}] + entries


def _short(model_id: str) -> str:
    return model_id.rsplit('/', 1)[-1]


def resolve_model(kind: str, settings=None, model: str = '') -> str:
    """The model a kind will actually load: what was pinned, else that backend's
    own default. Kept in one place so a run's notes and the embedder it describes
    can never name different models."""
    if kind not in BACKENDS:
        return ''
    if model:
        return model
    if kind == 'fastembed':
        # RAGLAB_FASTEMBED_MODEL keeps working exactly as before.
        return getattr(settings, 'fastembed_model', None) or DEFAULT_FASTEMBED
    return BACKEND_DEFAULTS[kind]


def language_note(kind: str, model: str = '') -> str:
    """One line for a run's notes. A leaderboard row whose embedder could not
    read the corpus is not a result, and nothing else on the row says so."""
    hint = _HINTS.get(kind)
    if kind in BACKENDS and model:
        entry = _MODELS.get(model)
        coverage = entry.languages if entry else 'coverage not recorded'
        return f'embedder {kind} on {model}: {coverage}'
    coverage = hint.languages if hint else 'coverage not recorded'
    return f'embedder {kind}: {coverage}'


def make_embedder(kind: str, settings=None, model: str = ''):
    if kind == 'ascii-hash':
        embedder = HashEmbedder()
        embedder.name = 'ascii-hash'          # type: ignore[attr-defined]
        embedder.dim = 128                    # type: ignore[attr-defined]
        return embedder
    if kind == 'token-hash':
        return TokenHashEmbedder()
    if kind == 'char-hash':
        return CharHashEmbedder()
    if kind in BACKENDS:
        # '' means "this backend's default", exactly as '' means "follow
        # RAGLAB_MODEL" for the chat roles: the lab pins nothing of its own.
        name = resolve_model(kind, settings, model)
        entry = _MODELS.get(name)
        prefixes = {'query_prefix': entry.query_prefix if entry else '',
                    'passage_prefix': entry.passage_prefix if entry else ''}
        if kind == 'fastembed':
            return FastEmbedMultilingual(name, **prefixes)
        if kind == 'sentence-transformers':
            return SentenceTransformerEmbedder(name, **prefixes)
        return OpenAIEmbedder(name, settings)
    raise ValueError(f'unknown lab embedder: {kind!r}')
