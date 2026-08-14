"""The topic-change nudge.

`topics.py` decides whether the message about to be sent belongs to the
conversation it is being sent into. It never acts on the answer: the browser
offers "start a new chat" and the user decides. So what these tests hold is the
*decision*, and above all the two things that must never happen — nagging on a
first message, and standing between the user and their assistant when the
embedder is unavailable.

Contract under test:

- `DriftVerdict(changed, score, reason)` — a dataclass, not a bare bool, because
  the UI says why it asked and a calibration run has to be readable.
- `is_opener(text)` — a bare greeting carrying no subject. Pure pattern
  matching, no model, so the e2e suite (BRAIN_EMBEDDER=fake) can drive the whole
  nudge by typing "hi".
- `detect_drift(recent, incoming, embeddings)` — `recent` is this session's
  recent user messages, oldest first. Empty means the first message of a chat,
  which can never drift. Otherwise: opener first, then cosine distance between
  `incoming` and the centroid of `recent`, against DRIFT_DISTANCE.
- Any failure of the embedder is `changed=False` — fail open. A broken detector
  must not block a turn.

The threshold itself is NOT decided here: these vectors are orthogonal on
purpose, so the test passes for any cut-off in (0, 1). What DRIFT_DISTANCE
should be is a measurement, and it lives in brain/tests/evals/.
"""
import pytest
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app
from lodestar_brain.topics import DRIFT_DISTANCE, DriftVerdict, detect_drift, is_opener

TRIP = ['help me plan the berlin trip in october',
        'which hotel near mitte should i book']
TAXES = 'i need to file the tax return before friday'


class StubEmbeddings:
    """Two orthogonal subjects, so a distance has something to measure.

    A stub rather than the `fake` embedder: LexicalHashEmbeddings would give
    these texts *some* distance, and a test whose pass depends on where a hash
    happens to land is a test that will fail for an unrelated reason later.
    """

    def __init__(self):
        self.calls = 0

    def _vector(self, text):
        self.calls += 1
        return [0.0, 1.0] if 'tax' in text else [1.0, 0.0]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


class BrokenEmbeddings:
    def embed_documents(self, texts):
        raise RuntimeError('the embedding model is not downloaded')

    def embed_query(self, text):
        raise RuntimeError('the embedding model is not downloaded')


# This is a unit test.
def test_an_opener_is_drift_but_a_substantive_question_is_not():
    # The user's own example. "hi" carries no subject, so continuing the
    # previous one is the wrong default — and this is the signal that needs no
    # model at all, which is what lets the offline e2e suite drive the nudge.
    for greeting in ('hi', 'Hi!', 'hello', 'hey', 'سلام', '  yo  '):
        assert is_opener(greeting), greeting
    # A greeting that carries a question is NOT an opener: nudging here would
    # interrupt someone who is being polite about a real request. The patterns
    # stay tight for the same reason signals_no_audio's do — a false positive
    # costs a click, and there is no upside to guessing.
    for real in ('hi, can you check the berlin visa rules?',
                 'hello — what did we decide about the flat?',
                 'highlight the overdue habits',   # starts with "hi"
                 'file the tax return'):
        assert not is_opener(real), real

    # And it is decided before the embedder is consulted, so a greeting is
    # caught even when retrieval is unavailable.
    verdict = detect_drift(TRIP, 'hi', BrokenEmbeddings())
    assert verdict.changed is True
    assert verdict.reason == 'opener'


# This is a unit test.
def test_the_first_message_of_a_chat_can_never_drift():
    embeddings = StubEmbeddings()
    verdict = detect_drift([], 'anything at all, even a total non sequitur', embeddings)
    assert verdict.changed is False
    assert verdict.reason == 'first-message'
    assert embeddings.calls == 0, (
        'there is nothing to drift from, so the check must not spend an '
        'embedding — this is also the request the browser skips entirely')
    # An opener opening a brand-new chat is the normal way to start one. It must
    # not be nudged either, or every new chat begins with a question about
    # whether to start a new chat.
    assert detect_drift([], 'hi', embeddings).changed is False


# This is a unit test.
def test_distance_decides_when_the_message_is_substantive():
    same = detect_drift(TRIP, 'and what about the trains to berlin?', StubEmbeddings())
    assert same.changed is False
    assert same.reason == 'same-topic'
    assert same.score < DRIFT_DISTANCE

    changed = detect_drift(TRIP, TAXES, StubEmbeddings())
    assert changed.changed is True
    assert changed.reason == 'distance'
    assert changed.score >= DRIFT_DISTANCE
    # The centroid is what is compared against, not the single last message:
    # one aside in an otherwise coherent chat must not make the next on-topic
    # message look like a new subject.
    assert detect_drift(TRIP + [TAXES], 'which hotel did i pick?',
                        StubEmbeddings()).changed is False


# This is a unit test.
def test_no_embedder_at_all_reports_no_measurement():
    # The route withholds the `fake` embedder: a hash of the words gives two
    # vectors a distance, but that distance is an artefact, not a similarity, so
    # judging semantic drift with it is a category error. Same answer as a broken
    # embedder because it is the same situation — nobody measured anything.
    verdict = detect_drift(TRIP, TAXES, None)
    assert verdict.changed is False
    assert verdict.reason == 'unavailable'
    # The opener signal is unaffected: it needs no model at all, which is what
    # keeps the nudge working — and testable — offline.
    assert detect_drift(TRIP, 'hi', None).reason == 'opener'


# This is a unit test.
def test_a_broken_embedder_lets_the_turn_through():
    # Fail open, deliberately. The embedding model downloads on first use and
    # weighs 2.2 GB; a laptop that has not fetched it yet must still be able to
    # talk to its own assistant.
    verdict = detect_drift(TRIP, TAXES, BrokenEmbeddings())
    assert isinstance(verdict, DriftVerdict)
    assert verdict.changed is False
    assert verdict.reason == 'unavailable'


# This is a unit test.
def test_a_broken_embedder_warns_once_and_then_stays_quiet(caplog, monkeypatch):
    """Fail-open used to mean fail-silent: a *permanently* broken embedder was
    indistinguishable from a cold one, forever. One warning per process names
    the failure; repeats drop to debug, because a cold 2.2 GB download is a
    normal state of a working install and must not flood the log."""
    import lodestar_brain.topics as topics

    monkeypatch.setattr(topics, '_failure_logged', False, raising=False)
    with caplog.at_level('DEBUG', logger='lodestar_brain.topics'):
        detect_drift(TRIP, TAXES, BrokenEmbeddings())
        detect_drift(TRIP, TAXES, BrokenEmbeddings())

    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1, 'exactly one warning across two failures'
    assert 'drift' in warnings[0].getMessage()
    assert len([r for r in caplog.records if r.levelname == 'DEBUG']) == 1


# This is an integration test.
def test_the_route_answers_the_browser_and_never_raises():
    client = TestClient(create_app(Settings(
        llm_provider='fake', embedder='fake', board_api_url='http://board.test',
        chroma_url='')))

    res = client.post('/agent/topic-check',
                      json={'recent': list(TRIP), 'text': 'hi'})
    assert res.status_code == 200
    assert res.json() == {'changed': True, 'score': 1.0, 'reason': 'opener'}, (
        'an opener needs no embedder, so its score is not a measurement — it is '
        'reported as certain rather than as a distance nobody computed')

    first = client.post('/agent/topic-check', json={'recent': [], 'text': 'hi'})
    assert first.json()['changed'] is False

    # A malformed body is a refusal, not a 500: the browser calls this before
    # every turn and an exception here would be an unsendable message.
    assert client.post('/agent/topic-check', json={}).status_code == 422


# This is a configuration invariant.
def test_the_threshold_is_a_cosine_distance():
    # Cosine distance is 1 - cosine similarity, so a threshold outside (0, 2)
    # can only mean the module has changed metric without changing its name —
    # and a 0 or a 2 would nudge on everything or on nothing.
    assert 0 < DRIFT_DISTANCE < 2


# This is a unit test.
@pytest.mark.parametrize('recent', [[''], ['   ']])
def test_a_session_of_blank_messages_is_treated_as_empty(recent):
    # Not hypothetical: `recent` is built from the transcript, and a whitespace
    # message would embed to a meaningless vector that every real message looks
    # far from — nudging on the second message of every chat.
    assert detect_drift(recent, TAXES, StubEmbeddings()).reason == 'first-message'
