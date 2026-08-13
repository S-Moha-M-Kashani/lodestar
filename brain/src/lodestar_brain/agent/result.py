"""What a turn is, once it has happened.

`AgentResult` and `AgentStep` are the brain's own types — no framework message
reaches the HTTP route or the evals — and the readers below are the one place a
LangChain transcript is turned into one. They are pure functions over a message
list, which is what lets the step-limit path report the steps and the spend of a
run that never finished.

This module imports nothing from the agent package: `graph` reads it, and it
reads no part of `graph`.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from ..middleware.untrusted import result_of

STEP_LIMIT_REPLY = 'I hit my step limit before finishing — try a smaller request.'


@dataclass
class AgentStep:
    tool: str
    arguments: dict
    result: object


@dataclass
class AgentResult:
    reply: str
    steps: list[AgentStep] = field(default_factory=list)
    # What the turn spent, or None when the model reported nothing.
    usage: dict | None = None


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


def _calls_in(messages: list[BaseMessage]) -> Iterator[dict]:
    """Every tool call the model has requested, in the order it requested them."""
    for message in messages:
        if isinstance(message, AIMessage):
            yield from message.tool_calls


def _steps_from(messages: list[BaseMessage]) -> list[AgentStep]:
    """Pair each requested tool call with the ToolMessage that answered it.

    A call with no answer yet — the transcript was cut off at the step limit —
    is not a step: the old loop appended one only once the tool had run.

    Steps therefore come back in *request* order, which is what lets a streaming
    consumer pair them with `astream`'s 'calling' events by position. That holds
    because an unanswered call can only be a trailing one: the sole way to have
    one is the step limit ending the run.
    """
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps: list[AgentStep] = []
    for call in _calls_in(messages):
        answer = results.get(call['id'])
        if answer is None:
            continue
        # result_of, not the message content: the content is fenced text meant
        # for the model, and the Assistant needs the rows the tool returned.
        steps.append(AgentStep(tool=call['name'], arguments=dict(call['args']),
                               result=result_of(answer)))
    return steps


def _reply_from(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return _text(message)
    return ''


def _usage_from(messages: list[BaseMessage]) -> dict | None:
    """What the turn spent, summed over its model calls.

    A turn that used tools is several calls, and each one re-sends the transcript
    grown by the last tool's answer — so those input tokens really are paid
    again, and summing them is the bill rather than double counting.

    None instead of zeros when nothing was reported: a model that does not report
    usage and a turn that cost nothing are different facts, and a turn shown as
    "0 tokens" is a measurement nobody made.
    """
    totals = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    reported = False
    for message in messages:
        usage = getattr(message, 'usage_metadata', None)
        if not usage:
            continue
        reported = True
        for key in totals:
            totals[key] += usage.get(key, 0)
    return totals if reported else None


def _result_from(messages: list[BaseMessage], reply: str | None = None) -> AgentResult:
    """One place a turn's result is built.

    Every method returns twice — once normally, once for the step limit — so
    six construction sites is six chances for one path to report a field the
    others forget. `reply` is passed only for the step-limit path, where the
    transcript is abandoned but the steps and the spend still happened.
    """
    return AgentResult(reply=_reply_from(messages) if reply is None else reply,
                       steps=_steps_from(messages),
                       usage=_usage_from(messages))


__all__ = ['STEP_LIMIT_REPLY', 'AgentResult', 'AgentStep']
