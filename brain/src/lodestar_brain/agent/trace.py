"""What a turn really handed the model, captured where it is handed over.

The record in `assistant.db` is a conversation: what the user said, what came
back. That is the right shape for a transcript and the wrong shape for
debugging, because everything that decides the answer — the system prompt, the
tools the model asked for, what those tools returned — exists only inside a
turn, for the length of a turn.

**The capture point is `on_chat_model_start`, and that choice is the whole
design.** It is the callback LangChain fires with the exact list of messages it
is about to send, system message included; nothing else in this process has that
list. Every other candidate is a reconstruction:

- the browser's transcript has no system prompt and never saw a tool call;
- the graph's message state has the tool calls but not the prompt, and not the
  edits summarisation and context-trimming middleware make on the way out;
- rebuilding the envelope from `SYSTEM_PROMPT` plus history would produce a
  plausible request that was never sent, which is worse than showing nothing —
  a debugging surface that lies costs more than one that is absent.

A turn calls the model once per tool round, and each call carries everything the
last one did plus what the tools answered. So the LAST envelope of a turn is the
fullest one, and the tape is that envelope followed by the answer it produced.
`TurnTrace` keeps only that, which is also why it costs one list per turn rather
than one per model call.

This module imports nothing from the rest of the agent package, the `result.py`
rule: `graph` reads it, and it reads no part of `graph`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

# The four roles a trace entry can carry, and what LangChain's own message types
# map onto. `message.type` is the framework's word for the same thing, so the
# map is one line rather than a chain of isinstance checks — and an unknown type
# keeps its own name instead of being filed as something it is not.
_ROLES = {'system': 'system', 'human': 'human', 'ai': 'ai', 'tool': 'tool'}

# A trace is a debugging aid, not a store: an entry longer than this is elided
# with a marker rather than filed whole, so one pasted web page cannot turn the
# record into megabytes. Marked, always — the spec allows display trimming only
# where it says so.
CONTENT_LIMIT = 20_000
ELIDED = '\n… [trimmed by the trace at {limit} characters]'

_now_ms = lambda: int(time() * 1000)


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


def _trimmed(text: str) -> str:
    if len(text) <= CONTENT_LIMIT:
        return text
    return text[:CONTENT_LIMIT] + ELIDED.format(limit=CONTENT_LIMIT)


def _metadata(message: BaseMessage) -> dict:
    """What a reader needs beside the text: which tools were asked for, and
    which call an answer belongs to. Absent when there is nothing to say —
    an empty metadata object on every entry is noise in a JSON column."""
    meta: dict = {}
    if isinstance(message, AIMessage):
        if message.tool_calls:
            meta['tool_calls'] = [{'name': c['name'], 'args': dict(c['args']),
                                   'id': c.get('id', '')}
                                  for c in message.tool_calls]
        if message.usage_metadata:
            meta['usage'] = dict(message.usage_metadata)
    if isinstance(message, ToolMessage):
        meta['tool_call_id'] = message.tool_call_id
        if message.name:
            meta['name'] = message.name
    return meta


def _entry(seq: int, message: BaseMessage) -> dict:
    return {'seq': seq, 'role': _ROLES.get(message.type, message.type),
            'content': _trimmed(_text(message)), 'metadata': _metadata(message)}


@dataclass
class TurnTrace:
    """One turn's record, from before it starts to after it ends.

    Built by the route, handed to the agent, and read back by whoever files it.
    A trace exists as soon as the turn does — status `in_flight` — so a turn
    that hangs is inspectable while it hangs, which is the case a developer most
    wants to look at and the one a record written only at the end cannot show.
    """

    session_id: str = ''
    board_id: str = ''
    model: str = ''
    provider: str = ''
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: int = field(default_factory=_now_ms)
    ended_at: int | None = None
    status: str = 'in_flight'
    error: str = ''
    usage: dict | None = None
    # The last list the model was handed, and the message it answered with.
    envelope: list[BaseMessage] = field(default_factory=list)
    answer: BaseMessage | None = None

    def settle(self, answer: BaseMessage | None = None,
               usage: dict | None = None) -> None:
        self.answer = answer
        self.usage = usage
        self.status = 'completed'
        self.ended_at = _now_ms()

    def fail(self, error: str) -> None:
        """A turn that died. The envelope so far is kept and no answer is
        invented — an `ai` entry here would be a sentence nobody wrote."""
        self.error = str(error)
        self.status = 'failed'
        self.ended_at = _now_ms()

    def interrupt(self, answer: BaseMessage | None = None) -> None:
        """A turn that stopped without finishing — the step limit, today. Not
        `completed`: the reply that comes back is the brain's apology, not the
        model's answer, and a status that cannot tell those apart is a status
        that hides the interesting case."""
        self.answer = answer
        self.status = 'interrupted'
        self.ended_at = _now_ms()

    def as_dict(self) -> dict:
        entries = [_entry(i, m) for i, m in enumerate(self.envelope)]
        if self.answer is not None:
            entries.append(_entry(len(entries), self.answer))
        return {'trace_id': self.trace_id, 'session_id': self.session_id,
                'board_id': self.board_id, 'status': self.status,
                'model': self.model, 'provider': self.provider,
                'started_at': self.started_at, 'ended_at': self.ended_at,
                'error': self.error, 'usage': self.usage, 'entries': entries}


class TraceCollector(BaseCallbackHandler):
    """The one hook. Attached to the run's config as a callback, so it sees
    every model call the graph makes without any node knowing it is there.

    Keeps the newest envelope and nothing else: the earlier calls of a turn are
    prefixes of the last one, so storing them all would file the same messages
    two and three times over.
    """

    def __init__(self, trace: TurnTrace):
        self.trace = trace

    def on_chat_model_start(self, serialized: dict,
                            messages: list[list[BaseMessage]],
                            **kwargs: Any) -> None:
        # A list of lists: one per generation the runnable was asked for, and
        # the agent asks for exactly one.
        if messages and messages[0]:
            self.trace.envelope = list(messages[0])


def final_answer(messages: list[BaseMessage]) -> BaseMessage | None:
    """The message a turn ended on: the last AI message that asked for no tool.

    Taken from the run's own messages rather than from the envelope, because the
    envelope by definition stops one message short of it — the model had not
    written it yet when it was handed the list.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return message
    return None


__all__ = ['CONTENT_LIMIT', 'TraceCollector', 'TurnTrace', 'final_answer']
