"""The agent.

LangChain owns the loop and the tool schemas. What is ours is the brain's own
result type — so a framework type never reaches the HTTP route or the evals —
and the two mechanics `create_agent` does not give us for free: a graph per
model the picker can choose, and partial steps when the step limit is hit.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ToolErrorMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError

from .config import Settings
from .llm import make_chat_model
from .untrusted import PROMPT_RULE, UntrustedToolOutput, result_of

SYSTEM_PROMPT = """You are Lodestar's assistant — a research companion and coach \
for a personal life dashboard ("your compass for life"). The board \
holds everything in the user's life: work, marriage, family, health, music, \
reading, travel, home.

You can: research and draft answers (web_search + find_related, cite urls), \
operate the board (list/create/update cards), and break fuzzy questions into \
concrete sub-questions. Before proposing a card, look for an existing one with \
find_related — it is the only way to avoid a duplicate.

Board columns: inbox, in-progress, answered (shown to the user as Done). \
Card types: question, problem, task, idea, plan, habit. \
A habit is repeated rather than finished — it carries a frequency (daily, \
weekly, monthly, yearly) and how many times per period. You can propose one; \
you cannot record that the user did it. \
Categories (life areas) are the user's own registry — work, love, family, \
health, mind, music, travel, home, money by default, but they can add or \
remove areas, so check existing cards for the ids in use; '' = uncategorized. \
Importance/urgency: high, low, or empty.

What you cannot do, and what to say instead. You cannot delete or archive a \
card — there is no tool for it, so never say you deleted, removed or archived \
anything. When asked to get rid of one, say so plainly and offer the three real \
options: you can move it to Done (retired, off the board, history kept), or \
suggest an edit for them to save, or they can open the card themselves and use \
"Delete card", which moves it to the Trash — recoverable from the History panel \
until they choose "Delete permanently" there. Same shape for anything else you \
lack: name the limit in one sentence, then the way to get it done.

You also cannot write to the board directly. Creating a card proposes it for \
approval, and updating one sends a suggested edit the user opens, adjusts and \
saves. So say you have proposed or suggested something — never that you added \
or changed it.

What you see is ONE conversation, not everything the user has ever said. They \
keep separate chats and can start a new one at any time, so treat the messages \
in front of you as the whole subject: do not carry over a topic from somewhere \
else, and do not assume a new chat is a continuation of an older one. A very \
long conversation may arrive trimmed to its recent turns. \
Other conversations exist and are searchable with recall_chat — reach for it \
only when the user refers to something outside this chat, rather than to \
enrich a question that is already complete.

Rules: never invent card ids — look them up with list_cards or \
find_related first. Asked what the user's concerns, thoughts or day looked \
like, answer with daily_recap — it reads that day's cards and conversations — \
never from memory. When research produces an answer, offer to save it into the \
card's notes. Keep replies short and concrete.

""" + PROMPT_RULE
# The clause is appended rather than written out, so the prompt and the wrapper
# cannot disagree about what the fence looks like — see untrusted.py.

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


def _tool_error(exc: Exception, request: Any) -> str:
    """A raising tool becomes {'error': str(exc)} fed back to the model.

    `create_agent` lets tool exceptions escape the graph, so one unreachable
    board would turn into a 500 for the whole chat turn. `ToolErrorMiddleware`
    is opt-in — returning None would propagate — so this handles everything,
    which is what the hand-rolled loop did before it. It also serves the async
    path, since the middleware falls back to `on_error` when no `aon_error` is
    given, and astream is the path the route actually takes.

    The message is `str(exc)` rather than the exception's type: these are our
    own tools failing against the user's own board, and "board unreachable at
    127.0.0.1:3000" is what lets the model say something useful about it.
    """
    return json.dumps({'error': str(exc)})


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


class LodestarAgent:
    def __init__(self, *, settings: Settings, tools: list[BaseTool],
                 system_prompt: str = SYSTEM_PROMPT, max_steps: int = 8,
                 llm: BaseChatModel | None = None):
        self.settings = settings
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self._llm = llm            # tests inject a FakeChat; create_app never does
        self._graphs: dict[tuple[str, str], Any] = {}
        # Build the default graph now rather than on the first request: an
        # unknown BRAIN_LLM has to fail at boot (create_app builds the agent),
        # which is the no-auto-modes rule. Lazily, a typo would serve a healthy
        # /health and then 500 the first chat.
        self._graph(None)

    def _graph(self, model: str | None, provider: str | None = None):
        """One compiled graph per provider/model pair.

        ``create_agent`` binds its model at build time. The Assistant picker can
        intentionally move between the local Ollama backend and OpenRouter, so
        the provider is part of the cache key too: a model slug alone is not a
        complete destination.

        The cache is per agent, not per module: the tools and the prompt differ
        between apps and between tests, so a process-wide one would answer a
        call with a graph compiled for somebody else's tools.
        """
        key = (provider or self.settings.llm_provider, model or '')
        if key not in self._graphs:
            llm = self._llm or make_chat_model(self.settings, model, provider)
            # UntrustedToolOutput sits outside the error middleware, so a tool's
            # failure message is fenced too: the text in "board unreachable at …"
            # is not ours either.
            self._graphs[key] = create_agent(
                model=llm, tools=self.tools, system_prompt=self.system_prompt,
                middleware=[UntrustedToolOutput(),
                            ToolErrorMiddleware(_tool_error)])
        return self._graphs[key]

    def _run_config(self, session_id: str | None = None,
                    board_id: str | None = None) -> dict:
        # A step is a model turn plus a tool turn, and the run ends on a model
        # turn: 2n+1 nodes for n tool calls.
        #
        # `session_id` rides in `configurable` rather than in a tool argument so
        # a tool can know which conversation it is serving without the model
        # being able to name it — recall_chat uses it to skip the chat already in
        # front of the model. Absent when there is none, so a caller that names
        # no session behaves exactly as before sessions existed.
        #
        # `board_id` travels the same way and for a stronger version of the same
        # reason: which board the user is looking at is not the model's decision,
        # and a tool argument is something a model can get wrong or be talked
        # into. Absent means the board API's own default board.
        config: dict = {'recursion_limit': 2 * self.max_steps + 1}
        configurable = {}
        if session_id:
            configurable['session_id'] = session_id
        if board_id:
            configurable['board_id'] = board_id
        if configurable:
            config['configurable'] = configurable
        return config

    def run(self, messages: list[dict], model: str | None = None,
            provider: str | None = None,
            session_id: str | None = None,
            board_id: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            # Streamed, not invoked: GraphRecursionError carries no messages,
            # so this is the only way to still report the steps taken.
            for chunk in self._graph(model, provider).stream(
                    {'messages': messages}, config=self._run_config(session_id, board_id),
                    stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return _result_from(seen, STEP_LIMIT_REPLY)
        return _result_from(seen)

    async def arun(self, messages: list[dict], model: str | None = None,
                   provider: str | None = None,
                   session_id: str | None = None,
                   board_id: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            async for chunk in self._graph(model, provider).astream(
                    {'messages': messages}, config=self._run_config(session_id, board_id),
                    stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return _result_from(seen, STEP_LIMIT_REPLY)
        return _result_from(seen)

    async def astream(self, messages: list[dict], model: str | None = None,
                      provider: str | None = None,
                      session_id: str | None = None,
                      board_id: str | None = None
                      ) -> AsyncIterator[tuple[str, Any]]:
        """The same turn as `arun`, reported while it happens.

        Yields ('calling', dict) when a tool is requested, ('step', AgentStep)
        once it answers, ('token', str) as the model writes, and exactly one
        ('done', AgentResult) last. The final result is built the same way `arun`
        builds it, so a caller can render the stream and still trust `done` as
        the record of the turn.

        'calling' exists because a requested-but-unanswered call is deliberately
        not a step, so a tool that takes seconds — a web search — would otherwise
        emit nothing for the slowest stretch of the turn. It carries no result
        because there is none yet; the matching 'step' follows in request order.

        Tokens are *provisional*. Two reasons a consumer must replace what it
        accumulated with `done.reply` rather than keep it: text can arrive on an
        AIMessage that also requests tools (commentary before the work, not the
        answer), and the step-limit path abandons the transcript entirely for
        STEP_LIMIT_REPLY.
        """
        seen: list[BaseMessage] = []
        sent = 0
        announced: set[str] = set()
        try:
            async for mode, chunk in self._graph(model, provider).astream(
                    {'messages': messages}, config=self._run_config(session_id, board_id),
                    stream_mode=['values', 'messages']):
                if mode == 'values':
                    seen = chunk['messages']
                    for call in _calls_in(seen):
                        if call['id'] in announced:
                            continue
                        announced.add(call['id'])
                        yield 'calling', {'tool': call['name'],
                                          'arguments': dict(call['args'])}
                    steps = _steps_from(seen)
                    for step in steps[sent:]:
                        yield 'step', step
                    sent = len(steps)
                    continue
                # 'messages' carries every message the graph produces, tool
                # answers included. Filtering to AIMessage is what stops a tool's
                # JSON being pasted into the reply as if the model had said it.
                message, _metadata = chunk
                if isinstance(message, AIMessage) and (text := _text(message)):
                    yield 'token', text
        except GraphRecursionError:
            yield 'done', _result_from(seen, STEP_LIMIT_REPLY)
            return
        yield 'done', _result_from(seen)
