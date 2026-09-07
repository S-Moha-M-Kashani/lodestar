"""The map's arithmetic: the text a card is embedded as, and the two ways a set
of vectors becomes a set of dots.

This moved out of the browser on 2026-09-07. What the browser used to do it did
badly for one reason and well for another: badly, because it re-embedded every
card on every visit and ran a 350-iteration t-SNE on the paint thread; well,
because the *layout* was seeded and deterministic, so a card kept its spot. The
determinism is the part that has to survive the move, and it is what most of
this file is about.

Four things here can go quietly wrong and each one moves somebody's dots:

  1. **A fingerprint that does not match the browser's.** The board and the
     brain each decide whether a card's vector is stale, and they have to agree.
     The reference values below were produced by the JS itself (the command is
     in the docstring), so a Python mirror that drifts fails here rather than
     silently re-embedding the whole board on every visit — or, worse, never
     re-embedding an edited card.
  2. **A layout that depends on the order the cards arrived in.** "The same set
     is the same map" is a requirement, and the layout cache keys on the sorted
     pairs precisely so that it holds. A projection that reads insertion order
     would make the cache lie.
  3. **A degenerate set raising instead of being placed.** One card, two cards,
     and a board where every card reads identically are all real states, and the
     spec says each of them renders.
  4. **A t-SNE that is not reproducible.** Seeded from the PCA layout with a
     fixed generator, or the dots move every time the reader comes back to a
     filter — which is the one thing the per-set cache exists to prevent.

Offline with no extras (`BRAIN_EMBEDDER=fake` is not even needed — nothing here
embeds anything), which is what keeps the brain suite runnable with no download
and no socket.
"""
import httpx
import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.retrieval.projection import (
    card_fingerprint, map_card_text, project, text_hash, vector_model_id,
)
from lodestar_brain.server import create_app


# --- The text and the fingerprint -----------------------------------------

# This is a unit test.
def test_a_card_reads_as_the_map_reads_it():
    """Tags, the category's LABEL, the type, the title, the notes — in that
    order, blanks dropped. The label and not the id, which is the whole reason
    renaming a category invalidates the card's vector: a card in a different
    life area is not the same thought, and an embedding model can read "Music"
    where it cannot read "music-3f2"."""
    card = {'tags': ['guitar', 'music'], 'type': 'task',
            'title': 'Learn the intro to Blackbird',
            'notes': 'Fingerpicking, slowly.', 'category': 'music'}
    labels = {'music': 'Music'}
    assert map_card_text(card, labels) == (
        'guitar music Music task Learn the intro to Blackbird '
        'Fingerpicking, slowly.')

    # The labels lead because the sentence is truncated from the tail: a long
    # note must never be able to push a card's category out of its own vector.
    assert map_card_text(card, labels).startswith('guitar music Music')

    # A category the registry does not hold contributes nothing rather than
    # contributing its id — the same answer `catLabel` gives, and the same
    # answer `cleanCard` gives the field itself.
    assert 'unknown' not in map_card_text({**card, 'category': 'unknown'}, labels)

    # Every field is optional. A card is only ever guaranteed a title.
    assert map_card_text({'title': 'Just this'}, {}) == 'Just this'
    assert map_card_text({}, {}) == ''


# This is a unit test. The one assertion in this file that pins two languages
# against each other.
def test_the_fingerprint_is_the_browsers_own_hash():
    """FNV-1a over UTF-16 code units, base36 — `textHash` in
    js/lib/projection.js, mirrored here the way server.js mirrors
    js/core/schedule.js's validator, and for the same reason: the two ends have
    to agree and neither can import the other.

    Regenerate these three values with:

        node -e "function h(s){let x=2166136261;for(let i=0;i<s.length;i++){
        x^=s.charCodeAt(i);x=Math.imul(x,16777619);}return (x>>>0).toString(36);}
        console.log(h('...'))"
    """
    assert text_hash('') == 'ztntfp'
    assert text_hash('guitar music Music task Learn the intro to Blackbird '
                     'Fingerpicking, slowly.') == 'ah8z3k'
    # Non-Latin text, which is why the hash is over code units and not bytes:
    # this board holds Persian, and a byte-wise hash would agree with the JS on
    # ASCII and disagree everywhere else — passing every careless test.
    assert text_hash('ذهن idea می‌خوام یاد بگیرم') == 'v17zyr'

    # And the fingerprint is that hash of that text, in one step, so no caller
    # can compose the two halves differently.
    card = {'tags': ['guitar', 'music'], 'type': 'task',
            'title': 'Learn the intro to Blackbird',
            'notes': 'Fingerpicking, slowly.', 'category': 'music'}
    assert card_fingerprint(card, {'music': 'Music'}) == 'ah8z3k'
    # Renaming the category changes it, which is the behaviour card-embeddings
    # requires and the reason the label is in the text at all.
    assert card_fingerprint(card, {'music': 'Guitar'}) != 'ah8z3k'


# --- The layout ------------------------------------------------------------

def _spread(points):
    """How far the dots reach across the field, per axis."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(xs) - min(xs), max(ys) - min(ys)


# This is a unit test.
def test_pca_finds_the_data_axes_and_fills_the_field():
    """A set built along one axis has to come back spread along one axis. The
    vectors below vary only in dimension 0, so PC-1 is that dimension and PC-2
    is noise — and the answer is checked as *order along the axis* rather than
    as coordinates, because the sign of an eigenvector is arbitrary and pinning
    it would be pinning numpy's arithmetic rather than the layout."""
    ids = [f'c{i}' for i in range(5)]
    vecs = np.array([[float(i), 0.0, 0.0, 0.0] for i in range(5)])
    coords = project(ids, vecs, kind='pca')

    assert set(coords) == set(ids)
    xs = [coords[i][0] for i in ids]
    # Monotonic in one direction or the other: the input was, so the layout is.
    assert xs == sorted(xs) or xs == sorted(xs, reverse=True)
    # And it uses the field rather than huddling: the plotting fractions
    # normalizePoints produces run in [0.06, 0.94], so a full spread is ~0.88.
    assert _spread(coords.values())[0] > 0.8
    for x, y in coords.values():
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


# This is a unit test.
def test_the_same_set_is_the_same_map_whatever_order_it_arrives_in():
    """The requirement, asserted directly. PCA centres the matrix and reads its
    principal axes, neither of which depends on row order — but a layout that
    seeded itself from an index, or normalised against the first row, would pass
    every other test here and fail this one."""
    rng = np.random.default_rng(7)
    ids = [f'c{i}' for i in range(9)]
    vecs = rng.normal(size=(9, 6))

    forward = project(ids, vecs, kind='pca')
    order = list(reversed(range(9)))
    backward = project([ids[i] for i in order], vecs[order], kind='pca')

    for card_id in ids:
        assert forward[card_id] == pytest.approx(backward[card_id], abs=1e-9)


# This is a unit test.
def test_a_degenerate_set_is_placed_rather_than_refused():
    """No cards, one card, two cards, and a board whose cards all read the same
    are four real states of the map, and the spec says every one of them
    renders. A single centred dot, two placed dots, and — for a set with no
    spread at all — the ring `normalizePoints` already falls back to, so
    identical cards are still distinguishable instead of stacking into one."""
    assert project([], np.zeros((0, 4)), kind='pca') == {}

    one = project(['solo'], np.array([[1.0, 2.0, 3.0, 4.0]]), kind='pca')
    assert one['solo'] == (0.5, 0.5)

    two = project(['a', 'b'], np.array([[0.0, 0.0], [1.0, 1.0]]), kind='pca')
    assert set(two) == {'a', 'b'} and two['a'] != two['b']

    same = np.ones((4, 3))
    ring = project(['a', 'b', 'c', 'd'], same, kind='pca')
    assert len({tuple(p) for p in ring.values()}) == 4, 'identical cards still separate'
    for x, y in ring.values():
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0

    # t-SNE degenerates below four points, so it stands PCA in rather than
    # dividing by a perplexity it cannot reach.
    assert project(['solo'], np.array([[1.0, 2.0]]), kind='tsne')['solo'] == (0.5, 0.5)


# This is a unit test.
def test_tsne_is_reproducible_and_separates_two_clusters():
    """Seeded, so the reader who returns to a filter finds their dots where they
    left them — the property the per-set cache is built on and the one thing a
    stochastic layout can silently lose.

    The full run, not a shortened one. Early exaggeration holds until iteration
    100, so a 60-iteration run is measured mid-squeeze with the clusters still
    interleaved — it passes the determinism half and fails the structure half,
    for a reason that is about the schedule and not about the code. Twelve
    points at 350 iterations is milliseconds.
    """
    rng = np.random.default_rng(3)
    left = rng.normal(loc=-4.0, scale=0.2, size=(6, 5))
    right = rng.normal(loc=4.0, scale=0.2, size=(6, 5))
    ids = [f'c{i}' for i in range(12)]
    vecs = np.vstack([left, right])

    first = project(ids, vecs, kind='tsne')
    second = project(ids, vecs, kind='tsne')
    for card_id in ids:
        assert first[card_id] == pytest.approx(second[card_id], abs=1e-12)

    # The two clusters are further from each other than either is from itself:
    # a t-SNE that had not converged, or had lost its P matrix, would scatter
    # them and fail this without failing determinism.
    import math
    pts = [first[i] for i in ids]
    within = max(math.dist(pts[i], pts[j]) for i in range(6) for j in range(i + 1, 6))
    across = min(math.dist(pts[i], pts[j]) for i in range(6) for j in range(6, 12))
    assert across > within


# This is a unit test: the port's fidelity, pinned where fidelity is actually
# achievable — which is not where it was assumed to be.
def test_the_port_matches_the_browser_early_and_cannot_match_it_late():
    """Measured 2026-09-07 against the JS run on the same vectors and the same
    PCA initialisation: 1.4e-14 after one iteration, 3.8e-11 after five, 1.8e-8
    after twenty, visibly parted by one hundred.

    The divergence is not a porting bug and no port can avoid it: the gains
    heuristic branches on `(gradient > 0) == (previous step > 0)`, a discrete
    test on a sign, so 1e-15 in a coordinate near zero sends the two runs down
    permanently different paths through a non-convex descent. What IS pinnable
    is that the first step agrees, which is what says the affinity matrix, the
    seeded noise, the gradient and the gains update are all the same arithmetic.
    A port that got the perplexity rule wrong — the browser floors it, and an
    unfloored 3.67 at twelve cards gives a different map — fails here at the
    fourteenth decimal rather than mysteriously three screens later.

    The reference below is the JS's own single-iteration output for the fixture
    in `_two_clusters`. Regenerate it by running `computeTsne` from
    js/views/overview.js with `ITER` set to 1.
    """
    from lodestar_brain.retrieval.projection import _pca2, _tsne
    rng = np.random.default_rng(3)
    vecs = np.vstack([rng.normal(loc=-4.0, scale=0.2, size=(6, 5)),
                      rng.normal(loc=4.0, scale=0.2, size=(6, 5))])
    first_step = _tsne(vecs, _pca2(vecs), iterations=1)
    assert first_step == pytest.approx(np.array(_JS_FIRST_STEP), abs=1e-12)


# One iteration of js/views/overview.js's computeTsne, on the fixture above.
_JS_FIRST_STEP = [
    [-0.49731170711439027, -0.2952582740420809],
    [-0.5656921934883389, 0.06294154071851221],
    [-0.34954774323859883, -0.004221642386502838],
    [-0.5236170206555538, 0.09205375749633103],
    [-0.6090392934967913, 0.01623513796800786],
    [-0.6602909539721742, 0.12177529542874314],
    [0.5307511962180556, 0.035917086162764504],
    [0.6008582998496622, -0.1497507378120154],
    [0.5901307730912079, -0.01774198009145825],
    [0.38786879705663596, 0.10927619931892914],
    [0.5257104938144311, -0.06610441254682649],
    [0.5701793519358543, 0.094878029785596],
]


# --- The two routes --------------------------------------------------------
#
# `/rag/vectors` fills what is missing, `/rag/projection` lays out what is
# asked for. They are separate because the set that needs embedding is the whole
# board and the set that needs laying out is whatever the filters left on
# screen — joining them would make changing a filter ask for embeddings.

BOARD = 'http://board.test'


def _app():
    return create_app(Settings(llm_provider='fake', embedder='fake',
                               transcriber='fake', board_api_url=BOARD))


def _card(card_id, title, notes='', category=''):
    return {'id': card_id, 'columnId': 'inbox', 'title': title, 'notes': notes,
            'type': 'question', 'category': category, 'importance': '',
            'urgency': '', 'num': 1, 'tags': [], 'createdAt': 1, 'updatedAt': 1,
            'doneAt': 0}


CATEGORIES = [{'id': 'music', 'label': 'Music', 'h': 120, 'position': 0}]


def _state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards,
                                     'categories': CATEGORIES})


class _CountingBoard:
    """A stand-in board that remembers the vectors written to it, so the next
    read sees them. Without that, "a second call embeds nothing" cannot be
    asserted at all — every call would meet an empty table."""

    def __init__(self):
        self.vectors: dict[str, dict] = {}
        self.writes = 0

    def read(self, request):
        return httpx.Response(200, json={'model': request.url.params.get('model'),
                                         'vectors': list(self.vectors.values())})

    def write(self, request):
        import json as _json
        body = _json.loads(request.content)
        self.writes += 1
        for v in body['vectors']:
            self.vectors[v['cardId']] = {**v, 'dim': len(v['vec'])}
        return httpx.Response(200, json={'model': body['model'],
                                         'written': len(body['vectors']),
                                         'refused': []})


# This is an integration test. The CardIndex manifest lesson, one layer out: an
# unchanged card is never embedded twice, and editing one card of five costs one
# embedding rather than five.
@respx.mock
def test_only_the_cards_whose_words_changed_are_embedded():
    cards = [_card(f'c{i}', f'Card {i}', category='music') for i in range(5)]
    board = _CountingBoard()
    state = respx.get(f'{BOARD}/api/state').mock(side_effect=lambda r: _state(cards))
    respx.get(f'{BOARD}/api/vectors').mock(side_effect=board.read)
    respx.put(f'{BOARD}/api/vectors').mock(side_effect=board.write)

    app = _app()
    with TestClient(app) as client:
        first = client.post('/rag/vectors').json()
        assert first['embedded'] == 5
        assert first['error'] is None

        # Nothing changed, so nothing is embedded — not on this visit, not on
        # the next one, and not from another machine reading the same board.
        again = client.post('/rag/vectors').json()
        assert again['embedded'] == 0
        assert again['reused'] == 5
        assert board.writes == 1, 'a board with nothing to embed is not written to'

        # One card's notes edited: exactly one embedding, asserted on what the
        # board was asked to store rather than on a count the route reports
        # about itself.
        cards[2] = {**cards[2], 'notes': 'Changed my mind about this one.'}
        edited = client.post('/rag/vectors').json()
        assert edited['embedded'] == 1
        assert board.writes == 2

        # And renaming a category invalidates that card's vector, which is what
        # the label being in the embedded text is FOR: a card in a different
        # life area is not the same thought.
        CATEGORIES[0]['label'] = 'Guitar'
        try:
            renamed = client.post('/rag/vectors').json()
            assert renamed['embedded'] == 5
        finally:
            CATEGORIES[0]['label'] = 'Music'
    assert state.called


# This is an integration test. The route answers partially rather than failing,
# because the map has somewhere to fall back to and this route exists to improve
# it rather than to gate it.
@respx.mock
def test_an_unavailable_embedder_names_what_is_missing_and_never_500s():
    cards = [_card('a', 'One'), _card('b', 'Two')]
    respx.get(f'{BOARD}/api/state').mock(return_value=_state(cards))
    respx.get(f'{BOARD}/api/vectors').mock(
        return_value=httpx.Response(200, json={'model': 'm', 'vectors': []}))

    with TestClient(_app()) as client:
        # The 2.2 GB first download, a stopped Ollama, a revoked key — all one
        # shape from here, and all of them a named answer rather than a 500.
        async def boom(self, _texts):
            raise RuntimeError('model not downloaded yet')

        from lodestar_brain.retrieval import LexicalHashEmbeddings
        LexicalHashEmbeddings.aembed_documents = boom
        try:
            res = client.post('/rag/vectors')
        finally:
            del LexicalHashEmbeddings.aembed_documents
        assert res.status_code == 200, 'a missing model must never 500 the map'
        body = res.json()
        assert body['embedded'] == 0
        assert sorted(body['missing']) == ['a', 'b']
        assert 'model not downloaded' in body['error']


# This is an integration test.
@respx.mock
def test_a_layout_is_cached_per_set_and_says_so():
    ids = [f'c{i}' for i in range(6)]
    rng = np.random.default_rng(11)
    stored = [{'cardId': i, 'fingerprint': f'fp-{i}', 'dim': 8,
               'vec': rng.normal(size=8).tolist()} for i in ids]
    respx.get(f'{BOARD}/api/vectors').mock(
        return_value=httpx.Response(200, json={'model': 'm', 'vectors': stored}))

    with TestClient(_app()) as client:
        first = client.post('/rag/projection', json={'ids': ids, 'kind': 'pca'}).json()
        assert first['cached'] is False
        assert set(first['coords']) == set(ids)

        # Switching a filter back is free, and the route says so rather than
        # leaving the browser to infer it.
        second = client.post('/rag/projection', json={'ids': ids, 'kind': 'pca'}).json()
        assert second['cached'] is True
        assert second['coords'] == first['coords']

        # The same cards in a different order are the same key: layout_key sorts
        # the pairs, which is what makes "the same set is the same map" a
        # property of the cache and not only of the arithmetic.
        shuffled = client.post('/rag/projection',
                               json={'ids': list(reversed(ids)), 'kind': 'pca'}).json()
        assert shuffled['cached'] is True
        assert shuffled['coords'] == first['coords']

        # A different projection is a different layout, not a cache hit.
        assert client.post('/rag/projection',
                           json={'ids': ids, 'kind': 'tsne'}).json()['cached'] is False

        # A card with no stored vector is NAMED and left out, never given a
        # placeholder position — a dot somewhere nothing computed is worse than
        # a dot that has not arrived.
        partial = client.post('/rag/projection',
                              json={'ids': [*ids, 'nope'], 'kind': 'pca'}).json()
        assert partial['missing'] == ['nope']
        assert 'nope' not in partial['coords']

        # And a projection nothing implements raises rather than falling back to
        # one the reader did not ask for — the seam rule, everywhere.
        assert client.post('/rag/projection',
                           json={'ids': ids, 'kind': 'umap'}).status_code == 400


# This is a unit test: the name the vectors are filed under has to change when
# the vector space does, and not otherwise.
def test_the_vector_namespace_names_the_embedder():
    from lodestar_brain.retrieval import LexicalHashEmbeddings
    one = LexicalHashEmbeddings()
    assert vector_model_id(one) == vector_model_id(LexicalHashEmbeddings())
    assert vector_model_id(one).startswith('LexicalHashEmbeddings:')
    # A different vector space is a different namespace, so switching the
    # brain's embedder invalidates nothing — it starts filling a second one, and
    # the first is still correct for the model that wrote it.
    other = LexicalHashEmbeddings(n=64)
    assert vector_model_id(other) != vector_model_id(one)
