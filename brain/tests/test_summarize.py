"""What a long conversation costs, and the reconciliation it reshapes.

Two hazards, and they are opposite. Summarisation that never fires leaves the
token complaint exactly where it was; summarisation that fires on an ordinary
exchange rewrites a conversation nobody asked it to rewrite. So the first test
asserts both directions against one threshold.

The second is about the seam between summarisation and `_turn_input`. A
summarised thread no longer holds a *prefix* of the browser's transcript — it
holds a window, with a summary standing in for everything before it — and a
reconciliation that only knows about prefixes reads that as a diverged history
and replaces the thread wholesale. The summary would be paid for and discarded on
the very next turn, every turn, which is worse than not summarising at all.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.agent.graph import _turn_input
from lodestar_brain.config import Settings
from lodestar_brain.llm import FakeChat

# SummarizationMiddleware's own wording for the message it leaves behind. Pinned
# here rather than paraphrased: it is how the test tells a summarised transcript
# from a merely short one.
SUMMARY_MARK = 'Here is a summary of the conversation to date'


class RecordingChat(FakeChat):
    """Keeps every transcript it was handed, so a test can assert on what the
    *model* saw rather than on what came back."""

    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **kwargs)


def _agent(chat, **overrides):
    settings = Settings(llm_provider='fake', summary_tokens=300, summary_keep=4,
                        clear_tools_tokens=0, **overrides)
    return LodestarAgent(settings=settings, tools=[], system_prompt='sys',
                         llm=chat)


def _long_conversation(turns=10):
    """A conversation big enough to cross a 300-token trigger, which is roughly
    1 200 characters — so twenty messages of a hundred or so."""
    talk = []
    for n in range(turns):
        talk.append({'role': 'user',
                     'content': f'question {n}: ' + 'about the move to berlin ' * 5})
        talk.append({'role': 'assistant',
                     'content': f'answer {n}: ' + 'here is what i found ' * 5})
    return talk


# This is an integration test: a whole turn through the real middleware stack.
def test_a_long_conversation_is_summarised_and_a_short_one_is_left_alone():
    """The threshold, asserted from both sides.

    Past it, the model must be handed a summary and fewer messages than were
    sent — otherwise nothing was saved. Short of it, the model must be handed the
    conversation untouched, because a summary of a two-line exchange spends a
    model call to lose information.
    """
    long_talk = _long_conversation()
    chat = RecordingChat(script=[AIMessage(content='summary of the move'),
                                 AIMessage(content='answered')])
    _agent(chat).run(long_talk)
    handed = chat.seen[-1]        # the answering call; the summariser's is first
    assert any(SUMMARY_MARK in str(m.content) for m in handed), (
        'a conversation past the trigger reached the model unsummarised')
    assert len(handed) < len(long_talk), 'summarising saved nothing'

    short = RecordingChat(script=[AIMessage(content='answered')])
    _agent(short).run([{'role': 'user', 'content': 'hello'}])
    handed = short.seen[-1]
    assert not any(SUMMARY_MARK in str(m.content) for m in handed)
    assert [m.content for m in handed if isinstance(m, HumanMessage)] == ['hello']
    # One model call, not two: the summariser was never asked.
    assert len(short.seen) == 1


# This is a unit test: the reconciliation, on the shape summarisation leaves.
def test_a_summarised_thread_is_still_recognised_as_this_conversation():
    """The thread holds a window of the browser's transcript, not a prefix.

    Everything before the summary is gone from the thread and still present in
    the browser, so a prefix comparison finds message one where message five is
    and gives up. It must find the window instead and feed only what follows —
    and it must still give up when the history has really moved, which is the
    second half of this test.
    """
    prior = [HumanMessage(content=f'{SUMMARY_MARK}:\n\nthey are moving',
                          additional_kwargs={'lc_source': 'summarization'}),
             HumanMessage(content='question 2'), AIMessage(content='answer 2')]
    incoming = [{'role': 'user', 'content': 'question 1'},
                {'role': 'assistant', 'content': 'answer 1'},
                {'role': 'user', 'content': 'question 2'},
                {'role': 'assistant', 'content': 'answer 2'},
                {'role': 'user', 'content': 'question 3'}]

    payload, before = _turn_input(prior, incoming)
    assert payload['messages'] == [{'role': 'user', 'content': 'question 3'}]
    assert before == {m.id for m in prior}, 'the thread was kept, not replaced'

    # And a history that really moved is still replaced wholesale: the thread
    # remembers an answer the browser no longer carries.
    edited = [m for m in incoming if m['content'] != 'answer 2']
    payload, before = _turn_input(prior, edited)
    assert isinstance(payload['messages'][0], RemoveMessage)
    assert before == set()


if __name__ == '__main__':
    pytest.main([__file__])
