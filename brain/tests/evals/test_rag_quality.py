import json
from pathlib import Path

import pytest

from lodestar_brain.rag.embedder import HashEmbedder
from lodestar_brain.rag.index import LeidenIndex

FIXTURE = Path(__file__).parent / "fixtures" / "rag_cards.json"


def _load():
    data = json.loads(FIXTURE.read_text())
    cards, label_of = [], {}
    for cluster, items in data["clusters"].items():
        for card in items:
            cards.append(card)
            label_of[card["id"]] = cluster
    return data, cards, label_of


@pytest.mark.eval
def test_find_related_precision_at_k_meets_threshold():
    data, cards, label_of = _load()
    index = LeidenIndex(HashEmbedder())
    index.build(cards)
    threshold = data["thresholds"]["precision_at_k"]

    for q in data["queries"]:
        results = index.query(q["text"], k=q["k"])
        assert results, f"no results for {q['text']!r}"
        # Precision@k: fraction of returned cards whose true cluster matches.
        hits = sum(1 for r in results if label_of.get(r["card"]["id"]) == q["expected_cluster"])
        precision = hits / len(results)
        assert precision >= threshold, (
            f"query {q['text']!r}: precision {precision:.2f} < {threshold}")


@pytest.mark.eval
def test_top_result_is_from_expected_cluster():
    data, cards, label_of = _load()
    index = LeidenIndex(HashEmbedder())
    index.build(cards)
    for q in data["queries"]:
        top = index.query(q["text"], k=q["k"])[0]
        assert label_of[top["card"]["id"]] == q["expected_cluster"], (
            f"top hit for {q['text']!r} was {top['card']['id']}")


@pytest.mark.eval
def test_communities_separate_the_two_topics():
    _, cards, label_of = _load()
    index = LeidenIndex(HashEmbedder())
    index.build(cards)
    comms = index.communities()
    # Expect the two topics to not be fully merged into a single community.
    assert len(comms) >= 2, f"expected >=2 communities, got {len(comms)}"
