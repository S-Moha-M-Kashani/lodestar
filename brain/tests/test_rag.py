import httpx
import pytest
import respx

from lodestar_brain.rag.embedder import HashEmbedder, make_embedder
from lodestar_brain.rag.index import LeidenIndex, make_retrieve_tool
from lodestar_brain.tools.board import BoardClient


def card(id, title, notes='', tags=None):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': notes,
            'type': 'question', 'category': '', 'importance': '', 'urgency': '', 'num': 1,
            'tags': tags or [], 'createdAt': 1, 'updatedAt': 1}


# two lexically distinct topic groups (HashEmbedder works on token overlap)
KUBE = [card('k1', 'How to scale kubernetes pods under load?'),
        card('k2', 'Best kubernetes pod autoscaling strategy?'),
        card('k3', 'Debug kubernetes pod restarts and scale limits')]
HIRE = [card('h1', 'Structure hiring interviews for senior candidates?'),
        card('h2', 'What hiring interview questions reveal candidates?'),
        card('h3', 'How to calibrate hiring interview feedback?')]


def test_hash_embedder_normalized_and_deterministic():
    e = HashEmbedder()
    v = e.embed(['kubernetes pods', 'kubernetes pods'])
    assert v.shape[0] == 2
    assert abs(float((v[0] * v[0]).sum()) - 1.0) < 1e-5
    assert float((v[0] * v[1]).sum()) > 0.999


def test_make_embedder_is_explicit_and_rejects_auto():
    assert isinstance(make_embedder('hash'), HashEmbedder)
    # 'auto' used to mean "fastembed, or HashEmbedder if the extra is missing",
    # which meant a machine without fastembed ran the toy embedder for months
    # without ever saying so. An unknown kind is now a boot-time error instead.
    for kind in ('auto', 'nonsense'):
        with pytest.raises(ValueError):
            make_embedder(kind)


def test_leiden_groups_topics_and_query_ranks():
    index = LeidenIndex(HashEmbedder())
    index.build(KUBE + HIRE)
    m = index.membership
    assert m[0] == m[1] == m[2]          # kubernetes cluster
    assert m[3] == m[4] == m[5]          # hiring cluster
    assert m[0] != m[3]
    top = index.query('kubernetes pod scaling', k=2)
    assert {r['card']['id'] for r in top} <= {'k1', 'k2', 'k3'}
    assert all(r['community'] == m[0] for r in top)
    comms = index.communities()
    assert sorted(c['size'] for c in comms) == [3, 3]


def test_empty_board_is_safe():
    index = LeidenIndex(HashEmbedder())
    index.build([])
    assert index.query('anything') == []
    assert index.communities() == []


@respx.mock
def test_find_related_tool_rebuilds_from_board():
    respx.get('http://board.test/api/state').mock(return_value=httpx.Response(
        200, json={'version': 1, 'cards': KUBE + HIRE}))
    tool = make_retrieve_tool(LeidenIndex(HashEmbedder()), BoardClient('http://board.test'))
    assert tool.name == 'find_related'
    out = tool.run({'text': 'interview feedback for candidates', 'k': 2})
    assert {r['card']['id'] for r in out} <= {'h1', 'h2', 'h3'}
