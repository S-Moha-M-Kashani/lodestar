"""The agent: one compiled graph per model the picker can offer.

LangChain owns the loop and the tool schemas. What is ours are the two mechanics
`create_agent` does not give for free — a graph cached per provider/model pair,
and partial steps when the step limit is hit — plus the middleware order, which
is load-bearing: the fence sits outside the error handler, so a tool's failure
message is fenced too, and the two schemas in `state.py`: what the graph keeps
and what the request carries.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError

from ..config import Settings
from ..llm import make_chat_model
from ..middleware.errors import ToolErrorMiddleware, _tool_error
from ..middleware.untrusted import UntrustedToolOutput
from .prompt import SYSTEM_PROMPT
from .result import (STEP_LIMIT_REPLY, AgentResult, _calls_in, _result_from,
                     _steps_from, _text)
from .state import LodestarState, TurnContext


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
                            ToolErrorMiddleware(_tool_error)],
                state_schema=LodestarState, context_schema=TurnContext)
        return self._graphs[key]

    def _run_config(self, session_id: str | None = None,
                    board_id: str | None = None) -> dict:
        # A step is a model turn plus a tool turn, and the run ends on a model
        # turn: 2n+1 nodes for n tool calls.
        #
        # `board_id` rides in `configurable` rather than in a tool argument:
        # which board the user is looking at is not the model's decision, and a
        # tool argument is something a model can get wrong or be talked into.
        # Absent means the board API's own default board.
        #
        # The *session* used to travel the same way and no longer does — it is
        # `TurnContext`, passed to the run and read off `ToolRuntime.context`.
        # Typed, so a misspelt key is an error rather than a silently absent
        # session, and stripped from the tool schema by the framework rather
        # than by `args_schema` merely happening not to mention it.
        config: dict = {'recursion_limit': 2 * self.max_steps + 1}
        if board_id:
            config['configurable'] = {'board_id': board_id}
        return config

    def _run_context(self, session_id: str | None) -> TurnContext:
        """Which conversation this turn belongs to, for the tools that need it.

        Every run method builds it the same way, so there is one answer to "what
        does a turn with no session look like" — the empty string, which
        `recall_chat` reads as "exclude nothing".
        """
        return TurnContext(session_id=session_id or '')

    def run(self, messages: list[dict], model: str | None = None,
            provider: str | None = None,
            session_id: str | None = None,
            board_id: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            # Streamed, not invoked: GraphRecursionError carries no messages,
            # so this is the only way to still report the steps taken.
            for chunk in self._graph(model, provider).stream(
                    {'messages': messages, 'session_id': session_id or ''},
                    config=self._run_config(session_id, board_id),
                    context=self._run_context(session_id),
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
                    {'messages': messages, 'session_id': session_id or ''},
                    config=self._run_config(session_id, board_id),
                    context=self._run_context(session_id),
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
                    {'messages': messages, 'session_id': session_id or ''},
                    config=self._run_config(session_id, board_id),
                    context=self._run_context(session_id),
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


__all__ = ['LodestarAgent']
