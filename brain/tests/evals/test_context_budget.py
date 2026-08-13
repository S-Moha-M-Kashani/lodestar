"""Calibration for the two context-budget thresholds.

`summarize.py` ships `CLEAR_TOOLS_TOKENS = 4 000` and `SUMMARY_TOKENS = 8 000`
with the second deliberately double the first, on the stated reasoning that "by
the time [summarisation] fires, dropping tool output has already been tried and
was not enough". This measures whether that ordering is real.

    uv run --project brain pytest brain/tests/evals/test_context_budget.py \\
      -v -m calibration -s

`-s` matters — the report is the point of the run, not the pass/fail. Unlike the
drift calibration next door this needs no model and no extra: what it measures is
*when each defence fires and what the request weighs*, which is arithmetic over
the message list, so a scripted `FakeChat` is not standing in for anything.

What it cannot measure is the other half — whether the answer survives being
summarised. That needs long real conversations and a live model, and the corpus
does not exist; `summarize.py` records the scan and states the experiment.

One honesty note about the numbers below. `SummarizationMiddleware` counts with
`count_tokens_approximately(..., use_usage_metadata_scaling=True)`, so under a
real provider the trigger is the char estimate rescaled by that provider's own
reported usage. `FakeChat` reports four characters to the token, which is the
estimate's own assumption, so the scaling factor here is ~1 and the figures are
the unscaled approximation. A provider that tokenises Persian less kindly reaches
the same trigger on less conversation.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import (CLEAR_TOOLS_TOKENS, SUMMARY_KEEP,
                                   SUMMARY_TOKENS, Settings)
from lodestar_brain.llm import FakeChat

# SummarizationMiddleware's own wording, as in test_summarize.py: it is how a
# summarised transcript is told from a merely long one.
SUMMARY_MARK = 'Here is a summary of the conversation to date'

# One tool result, sized from the real board: 40 live cards carrying 2 190
# characters of title and notes, so `list_cards` weighs ~550 approximate tokens.
# The tool-heavy turn is the case the cheap defence exists for, and it is the
# ordinary one here — every board tool answers with the whole board.
TOOL_RESULT = 'card: the boiler has not been fixed, notes follow. ' * 44


@tool
def look_it_up(query: str) -> str:
    """Answer with a board-sized blob."""
    return TOOL_RESULT


class Recording(FakeChat):
    """Keeps every transcript it was handed, so the report can weigh the
    requests rather than the replies.

    It also answers the summariser off-script. `SummarizationMiddleware` invokes
    the model directly with a single prompt string — no system message, no
    history — which is both how its call is told apart from an agent turn here
    and why it must not be served from the script: popping a scripted tool call
    to stand in for a summary makes the run that summarises take a different
    path through the script from the run that does not, and the two columns of
    the report stop being comparable.
    """

    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if _is_summariser_call(messages):
            reply = AIMessage(content='they asked about the boiler report')
            return ChatResult(generations=[ChatGeneration(message=reply)])
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **kwargs)


def _is_summariser_call(messages):
    return len(messages) == 1 and isinstance(messages[0], HumanMessage)


def _replay(*, clear, summary, rounds=16):
    """One tool-heavy conversation through the real middleware stack.

    Returns (peak request tokens, summarised?, summariser calls). The script asks
    for a tool `rounds` times and then answers.
    """
    script = [AIMessage(content='', tool_calls=[{'name': 'look_it_up',
                                                 'args': {'query': f'q{i}'},
                                                 'id': f'c{i}'}])
              for i in range(rounds)]
    script += [AIMessage(content=f'reply {i}') for i in range(rounds)]
    chat = Recording(script=script)
    chat.seen = []
    settings = Settings(llm_provider='fake', summary_tokens=summary,
                        summary_keep=SUMMARY_KEEP, clear_tools_tokens=clear,
                        clear_tools_keep=3)
    agent = LodestarAgent(settings=settings, tools=[look_it_up],
                          system_prompt='sys', llm=chat, max_steps=rounds + 4)
    agent.run([{'role': 'user', 'content': 'what did the boiler report say'}])
    turns = [m for m in chat.seen if not _is_summariser_call(m)]
    peak = max(count_tokens_approximately(m) for m in turns)
    summarised = any(SUMMARY_MARK in str(m.content)
                     for msgs in turns for m in msgs)
    return peak, summarised, len(chat.seen) - len(turns)


# This is a calibration.
@pytest.mark.calibration
def test_the_cheap_defence_bounds_the_request_and_the_summariser_cannot_see_it():
    """Where the two thresholds actually sit relative to each other.

    The finding, and the reason this file exists: the two middlewares are hooked
    into different places. `SummarizationMiddleware` is a `before_model` state
    hook and counts the *thread*, which always holds every tool result in full.
    `ContextEditingMiddleware` is a `wrap_model_call` wrapper and edits a *copy*
    for the request. So the cheap defence's saving is real on the wire and
    invisible to the expensive defence's trigger — a conversation whose growth is
    all tool output is summarised anyway, lossily and for a model call, at the
    moment the uncleared thread crosses `SUMMARY_TOKENS`.
    """
    off, off_summarised, off_calls = _replay(clear=0, summary=0)
    cheap, cheap_summarised, cheap_calls = _replay(clear=CLEAR_TOOLS_TOKENS,
                                                   summary=0)
    both, both_summarised, both_calls = _replay(clear=CLEAR_TOOLS_TOKENS,
                                                summary=SUMMARY_TOKENS)
    dear, dear_summarised, dear_calls = _replay(clear=0, summary=SUMMARY_TOKENS)

    print(f'\ntool-heavy conversation, {len(TOOL_RESULT)}-char tool results, '
          f'approximate tokens')
    for name, peak, did, calls in (
            ('neither defence', off, off_summarised, off_calls),
            ('clearing only', cheap, cheap_summarised, cheap_calls),
            ('summariser only', dear, dear_summarised, dear_calls),
            (f'shipped {CLEAR_TOOLS_TOKENS}/{SUMMARY_TOKENS}', both,
             both_summarised, both_calls)):
        print(f'  {name:20s} peak request={peak:6d}  summarised={str(did):5s}'
              f'  summariser calls={calls}')
    print(f'  clearing alone saved {off - cheap} tokens off the peak and held it '
          f'{"under" if cheap < SUMMARY_TOKENS else "over"} SUMMARY_TOKENS;'
          f' the summariser then spent {both_calls} extra model call(s) to take'
          f' a further {cheap - both} off it')

    # The cheap defence does its job: on a conversation that grows by tool output
    # it holds the request below the summariser's own trigger, unaided.
    assert cheap < off, 'clearing tool results saved nothing'
    assert cheap < SUMMARY_TOKENS, (
        f'clearing alone left the request at {cheap} tokens, past SUMMARY_TOKENS '
        f'— the premise of this calibration no longer holds')

    # And the summariser fires anyway, because it never saw that saving. Pinned
    # rather than merely printed: if a LangChain upgrade ever lets the trigger
    # observe the edited request, the ratio comment in summarize.py starts being
    # true and this assertion is where that is noticed.
    assert both_summarised, (
        'the summariser no longer fires on a thread the context editor had '
        'already brought under its trigger — re-read the ratio note in '
        'summarize.py, it may now be describing reality')
    assert both_calls == dear_calls > 0, (
        'clearing changed how often the summariser ran, so its saving is now '
        'visible to the trigger — same news, same note')
    assert cheap_calls == off_calls == 0, 'the summariser ran with its knob at 0'
    # And what that call bought, on this conversation, is nothing: the peak
    # request under both defences is the peak under the cheap one alone. The
    # expensive defence paid a model call and rewrote turns the user may yet
    # refer back to, off a number the cheap defence had already brought down.
    assert both == cheap, (
        'summarising moved the peak request that clearing had already set — the '
        'headline figure in summarize.py needs re-measuring')


if __name__ == '__main__':
    pytest.main([__file__, '-s'])
