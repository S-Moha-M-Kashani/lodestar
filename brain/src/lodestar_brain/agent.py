"""The agent.

LangChain owns the loop and the tool schemas. What is ours is the brain's own
result type — so a framework type never reaches the HTTP route or the evals —
and the two mechanics `create_agent` does not give us for free: a graph per
model the picker can choose, and partial steps when the step limit is hit.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
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

Rules: never invent question ids — look them up with list_questions or \
find_related first. When you change the board, say exactly what you changed. \
When research produces an answer, offer to save it into the question's notes. \
Keep replies short and concrete."""

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


def _decode(content: Any) -> object:
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


def _steps_from(messages: list[BaseMessage]) -> list[AgentStep]:
    """Pair each requested tool call with the ToolMessage that answered it.

    A call with no answer yet — the transcript was cut off at the step limit —
    is not a step: the old loop appended one only once the tool had run.
    """
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    steps: list[AgentStep] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            answer = results.get(call['id'])
            if answer is None:
                continue
            steps.append(AgentStep(tool=call['name'], arguments=dict(call['args']),
                                   result=_decode(answer.content)))
    return steps


def _reply_from(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return _text(message)
    return ''


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
            self._graphs[key] = create_agent(
                model=llm, tools=self.tools, system_prompt=self.system_prompt,
                middleware=[ToolErrorMiddleware(_tool_error)])
        return self._graphs[key]

    @property
    def _config(self) -> dict:
        # A step is a model turn plus a tool turn, and the run ends on a model
        # turn: 2n+1 nodes for n tool calls.
        return {'recursion_limit': 2 * self.max_steps + 1}

    def run(self, messages: list[dict], model: str | None = None,
            provider: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            # Streamed, not invoked: GraphRecursionError carries no messages,
            # so this is the only way to still report the steps taken.
            for chunk in self._graph(model, provider).stream(
                    {'messages': messages}, config=self._config,
                    stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return AgentResult(reply=STEP_LIMIT_REPLY, steps=_steps_from(seen))
        return AgentResult(reply=_reply_from(seen), steps=_steps_from(seen))

    async def arun(self, messages: list[dict], model: str | None = None,
                   provider: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            async for chunk in self._graph(model, provider).astream(
                    {'messages': messages}, config=self._config,
                    stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return AgentResult(reply=STEP_LIMIT_REPLY, steps=_steps_from(seen))
        return AgentResult(reply=_reply_from(seen), steps=_steps_from(seen))

    async def astream(self, messages: list[dict], model: str | None = None,
                      provider: str | None = None
                      ) -> AsyncIterator[tuple[str, Any]]:
        """The same turn as `arun`, reported while it happens.

        Yields ('step', AgentStep) as each tool answers, ('token', str) as the
        model writes, and exactly one ('done', AgentResult) last. The final
        result is built the same way `arun` builds it, so a caller can render
        the stream and still trust `done` as the record of the turn.

        Tokens are *provisional*. Two reasons a consumer must replace what it
        accumulated with `done.reply` rather than keep it: text can arrive on an
        AIMessage that also requests tools (commentary before the work, not the
        answer), and the step-limit path abandons the transcript entirely for
        STEP_LIMIT_REPLY.
        """
        seen: list[BaseMessage] = []
        sent = 0
        try:
            async for mode, chunk in self._graph(model, provider).astream(
                    {'messages': messages}, config=self._config,
                    stream_mode=['values', 'messages']):
                if mode == 'values':
                    seen = chunk['messages']
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
            yield 'done', AgentResult(reply=STEP_LIMIT_REPLY,
                                      steps=_steps_from(seen))
            return
        yield 'done', AgentResult(reply=_reply_from(seen), steps=_steps_from(seen))
