"""The two retrieval tools, as the agent sees them.

`tools/retrieve.py` is deliberately thin: the pipeline lives in `retrieval.py`
and these wrappers decide only what the model is told. That contract is what
these tests hold — the shape of a result, not the quality of a ranking.
"""
import httpx
import respx
from langchain.tools import ToolRuntime

from lodestar_brain.agent import TurnContext
from lodestar_brain.retrieval import CardIndex, LexicalHashEmbeddings
from lodestar_brain.tools.board import BoardClient
from lodestar_brain.tools.retrieve import make_recall_tool, make_retrieve_tool


def runtime(context, config=None):
    """A `ToolRuntime` in the shape `ToolNode` builds one.

    A unit test calls the tool directly, so there is no graph to inject it —
    which is also the point: the tool must work when nothing injects anything.
    """
    return ToolRuntime(state={}, context=context, config=config or {},
                       stream_writer=lambda _: None, tool_call_id='t1',
                       store=None)


def card(id, title, tags=None):
    return {'id': id, 'columnId': 'inbox', 'title': title, 'notes': '',
            'type': 'question', 'category': '', 'importance': '', 'urgency': '',
            'num': 1, 'tags': tags or [], 'createdAt': 1, 'updatedAt': 1}


# Two lexically distinct subjects, so a ranking has something to get wrong.
CARDS = [
    card('k1', 'How to scale kubernetes pods under load?', ['infra']),
    card('k2', 'Best kubernetes pod autoscaling strategy?', ['infra']),
    card('k3', 'Debug kubernetes pod restarts and scale limits', ['infra']),
    card('h1', 'Structure hiring interviews for senior candidates?', ['team']),
    card('h2', 'What hiring interview questions reveal candidates?', ['team']),
    card('h3', 'How to calibrate hiring interview feedback?', ['team']),
]


def board_state(cards):
    return httpx.Response(200, json={'version': 1, 'cards': cards})


def index_and_board():
    return CardIndex(LexicalHashEmbeddings()), BoardClient('http://board.test')


class FakeStore:
    def __init__(self):
        self.asked = []

    def search(self, text, k=5, exclude_session=None, board_id=None):
        self.asked.append((text, k, exclude_session, board_id))
        return [{'text': 'the wifi password is hunter2', 'score': 0.9,
                 'metadata': {'role': 'user', 'created_day': 20260712,
                              'session_id': 's-old'}}]


# This is a unit test.
@respx.mock
def test_find_related_ranks_the_board_and_claims_no_score():
    route = respx.get('http://board.test/api/state').mock(
        return_value=board_state(CARDS))
    tool = make_retrieve_tool(*index_and_board())
    hits = tool.run({'text': 'kubernetes pod scaling', 'k': 2})
    assert len(hits) <= 2
    assert {hit['card']['id'] for hit in hits} <= {'k1', 'k2', 'k3'}
    assert [hit['rank'] for hit in hits] == list(range(1, len(hits) + 1))
    # RRF exposes no fused score, so a number invented from a rank would mean
    # nothing. `community` is asserted absent because it used to be here: a
    # reader still expecting it fails loudly instead of reading None as a theme.
    assert 'score' not in hits[0] and 'community' not in hits[0]
    # The brief is built from the board rows the tool just read, not from index
    # metadata: tags are a list there, and space-joining them so a store can
    # filter on them must not come back to the model as a lossy string.
    assert hits[0]['card']['tags'] == ['infra']
    assert set(hits[0]['card']) == {'id', 'title', 'columnId', 'tags'}
    # The board is re-read on every call, so a card added since the last one is
    # findable without anything having to invalidate a cache.
    route.mock(return_value=board_state(CARDS + [card('n1', 'kubernetes notes')]))
    assert 'n1' in {hit['card']['id']
                    for hit in tool.run({'text': 'kubernetes notes', 'k': 3})}


# This is an integration test (in-process Chroma, no server, no disk).
@respx.mock
def test_find_related_also_answers_from_the_chat_record():
    """find_related is the agent's one search over everything the user has:
    the board (board.db via the paired board API) AND the chat record
    (assistant.db, through its Chroma chunk index) — which board and which
    record follows from the brain's own wiring, so the :3001 test brain reads
    only test data. The chat side is *semantic*: «دعوا» must reach a chunk
    saying «دعوامون» even though they share no BM25 token — the recall box's
    lexical-evidence floor is for humans reading a result list, not for a
    model that judges relevance itself."""
    from lodestar_brain.retrieval import ChatStore, MEMORY_URL
    respx.get('http://board.test/api/state').mock(return_value=board_state(CARDS))
    index, client = index_and_board()
    store = ChatStore(MEMORY_URL, LexicalHashEmbeddings(),
                      collection='find-related-chat')
    store.index_messages([{'id': 1, 'role': 'user', 'createdAt': 1753995600000,
                           'content': 'دیشب دعوامون شد سر برنامه آخر هفته'}])

    tool = make_retrieve_tool(index, client, memory=store)
    hits = tool.run({'text': 'دعوا', 'k': 3})
    chat_rows = [hit for hit in hits if 'chat' in hit]
    assert chat_rows, 'find_related must also answer from the chat record'
    assert any('دعوامون' in hit['chat']['text'] for hit in chat_rows), (
        'a chunk related only semantically (no shared token) must reach '
        'the agent')
    assert {'text', 'role'} <= set(chat_rows[0]['chat']), (
        'the model needs the words and who said them')
    assert [hit['rank'] for hit in chat_rows] == list(
        range(1, len(chat_rows) + 1))
    # Cards keep their shape exactly — the Assistant's source list reads
    # row.card to build a clickable citation.
    assert all(set(hit) == {'card', 'rank'} or set(hit) == {'chat', 'rank'}
               for hit in hits)


# This is a unit test.
def test_recall_chat_hands_back_what_the_store_found():
    store = FakeStore()
    tool = make_recall_tool(store)
    assert tool.name == 'recall_chat'
    assert tool.args_schema.model_json_schema()['required'] == ['text']
    matches = tool.run({'text': 'wifi password', 'k': 3})
    assert 'hunter2' in matches[0]['text']
    assert store.asked == [('wifi password', 3, None, None)]


# This is a unit test.
def test_recall_skips_the_chat_it_is_already_reading():
    """The current conversation is already in the context window.

    Returning it back spends the model's attention re-reading what it can
    already see, and — worse — makes a fresh chat look like it has history the
    moment the model reaches for the tool. Which chat that is arrives through
    the run's typed context, not as a tool argument: the model must not be able
    to name (or spoof) it, and the args schema below is the assertion of that.
    """
    store = FakeStore()
    tool = make_recall_tool(store)
    config = {'configurable': {'board_id': 'b-work'}}

    # Injected into the call's arguments, which is exactly what ToolNode does
    # with a declared runtime — and the arguments the *model* supplied are
    # stripped of it first, so nothing here can be forged from a tool call.
    matches = tool.run({'text': 'wifi password',
                        'runtime': runtime(TurnContext(session_id='s-current'),
                                           config)})
    # The board travels the same way and for a stronger version of the same
    # reason: recall must not reach into another board's conversations, and
    # which board that is was the user's choice, not the model's.
    assert store.asked[-1] == ('wifi password', 5, 's-current', 'b-work')
    # Exactly the two parameters, asserted as a set rather than as two absences:
    # the session and the board are injected, and so is the runtime that carries
    # them — none of the three may appear as something the model can fill in.
    assert set(tool.args_schema.model_json_schema()['properties']) == {'text', 'k'}
    assert set(tool.tool_call_schema.model_json_schema()['properties']) == {'text', 'k'}

    # Dated, so the model can say "you mentioned this on the 12th" instead of
    # quoting the past as though it were said now. The date is already in the
    # chunk metadata; the chat's *title* is deliberately not carried — see the
    # spec's Recall section for why a copied title goes stale on rename.
    assert matches[0]['day'] == 20260712

    # Missing runtime is not an error: an eval or a curl runs without one, and a
    # recall that excluded nothing is strictly better than a 500.
    assert tool.run({'text': 'wifi password'})
    assert store.asked[-1][2] is None
    assert store.asked[-1][3] is None


# This is an integration test (the real agent graph, with a fake model and store).
def test_the_agent_hands_recall_the_session_the_request_named():
    """The test above injects the runtime by hand; this is the wiring that has to
    put it there.

    Which chat a turn belongs to reaches the tool through
    `create_agent(context_schema=TurnContext)` and a declared `ToolRuntime`. Not
    as a tool argument, which the model fills in — and no longer through
    `configurable`, which is what a checkpoint records.
    """
    from langchain_core.messages import AIMessage

    from lodestar_brain.agent import LodestarAgent
    from lodestar_brain.config import Settings
    from lodestar_brain.llm import FakeChat

    store = FakeStore()
    script = [AIMessage(content='', tool_calls=[
        {'name': 'recall_chat', 'args': {'text': 'wifi'}, 'id': 'c1'}]),
        AIMessage(content='you said it was hunter2')]
    agent = LodestarAgent(settings=Settings(llm_provider='fake'),
                          tools=[make_recall_tool(store)], system_prompt='sys',
                          llm=FakeChat(script=script))
    result = agent.run([{'role': 'user', 'content': 'what was the wifi password'}],
                       session_id='s-current', board_id='b-work')
    assert store.asked == [('wifi', 5, 's-current', 'b-work')]
    assert result.steps[0].arguments == {'text': 'wifi'}, (
        'the session is not among the arguments the model chose')
