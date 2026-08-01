import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from lodestar_brain.llm.fake import FakeChat
from lodestar_brain.retrieval import CardIndex, LexicalHashEmbeddings
from lodestar_brain.tools.retrieve import make_retrieve_tool

from .harness import InMemoryBoard

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
    index = CardIndex(LexicalHashEmbeddings())
    index.build(cards)
    threshold = data["thresholds"]["precision_at_k"]

    for q in data["queries"]:
        results = index.search(q["text"], k=q["k"])
        assert results, f"no results for {q['text']!r}"
        # Precision@k: fraction of returned cards whose true cluster matches.
        hits = sum(1 for r in results
                   if label_of.get(r.metadata["id"]) == q["expected_cluster"])
        precision = hits / len(results)
        assert precision >= threshold, (
            f"query {q['text']!r}: precision {precision:.2f} < {threshold}")


@pytest.mark.eval
def test_top_result_is_from_expected_cluster():
    data, cards, label_of = _load()
    index = CardIndex(LexicalHashEmbeddings())
    index.build(cards)
    for q in data["queries"]:
        top = index.search(q["text"], k=q["k"])[0]
        assert label_of[top.metadata["id"]] == q["expected_cluster"], (
            f"top hit for {q['text']!r} was {top.metadata['id']}")


# This is an eval.
@pytest.mark.eval
def test_lexical_scoring_beats_dense_alone_on_a_rare_literal():
    """Why the lexical half stays beside the vectors — and which part of it earns
    its keep, which is not the part you would guess.

    The error code is in exactly one card, buried in a long note, while a short
    card matches the rest of the query well. Measured on this fixture, the stages
    rank the four infra cards like this:

        dense only          i2, i4, h1
        BM25 only           i2, i4
        after RRF fusion    i2, i4
        after lexical rerank    i4, i2      <- coverage 0.629 vs 0.351

    So BM25 does **not** rescue the rare literal on its own, and neither does the
    fusion: BM25's length normalisation penalises the long note about as hard as
    cosine's dilution does. What puts the right card first is the IDF
    term-coverage reranker — the one component LangChain has no equivalent of,
    ported from the lab's `rerank.py`. Coverage asks what share of the query's
    IDF mass a card contains and does not care how much other text surrounds it,
    which is exactly the property the other two stages lack.

    Dense-only below is the same store and the same embeddings with only the
    lexical half removed, so the gap cannot be anything else. Deleting the
    reranker fails the second assertion."""
    data, cards, _ = _load()
    case = data["rare_literal"]
    index = CardIndex(LexicalHashEmbeddings())
    index.build(cards)

    def rank_of(ids):
        # Absent counts as one worse than last, so "never found" compares.
        return ids.index(case["expected_id"]) if case["expected_id"] in ids \
            else len(ids)

    hybrid = [d.metadata["id"] for d in index.search(case["query"], k=case["k"])]
    dense = [d.metadata["id"] for d in index.store.as_retriever(
        search_kwargs={"k": case["k"]}).invoke(case["query"])]

    assert rank_of(hybrid) < rank_of(dense), (
        f"hybrid ranked {case['expected_id']} at {rank_of(hybrid)} and dense at "
        f"{rank_of(dense)} — the lexical half bought nothing here, so either the "
        f"fixture no longer has a rare literal in it or BM25 dropped out of the "
        f"pipeline. hybrid={hybrid} dense={dense}")
    assert hybrid[0] == case["expected_id"], (
        f"the card naming the error is not the first hit: {hybrid}")


# This is an eval.
@pytest.mark.eval
def test_a_gate_that_rejects_everything_leaves_nothing_to_answer_from():
    """Candidate F's one addition after retrieval, at the seam that ships.

    An empty result is the honest answer when the board cannot support the
    question — without a gate every question gets contexts, including those. The
    third case is the one that breaks silently: a reply the gate cannot parse
    must degrade to the ungated pipeline, because NO_OPINION clears the
    threshold. Scoring an unreadable reply as zero would empty the context of
    every question the moment a model changed its output format."""
    _, cards, _ = _load()
    board = InMemoryBoard(cards)
    index = CardIndex(LexicalHashEmbeddings())
    query = "kubernetes cluster node scaling"

    def hits(reply):
        llm = FakeChat(script=[AIMessage(content=reply)]) if reply else None
        tool = make_retrieve_tool(index, board, llm=llm)
        return tool.invoke({"text": query, "k": 3})

    ungated = hits(None)
    assert ungated, "the board has matching cards, so the premise is wrong"

    rejected = hits("1: 0\n2: 0\n3: 0")
    assert rejected == [], (
        f"the gate scored every context 0 and they reached the model anyway: "
        f"{rejected}")

    unparsed = hits("I could not read those excerpts, sorry.")
    assert len(unparsed) == len(ungated), (
        f"an unparseable gate reply emptied the context ({len(unparsed)} of "
        f"{len(ungated)} survived) — a malformed reply must mean 'no opinion', "
        f"never 'irrelevant'")
