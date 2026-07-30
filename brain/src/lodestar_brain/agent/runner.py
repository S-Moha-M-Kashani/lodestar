"""The agent runner.

LangChain owns the loop and the tool schemas; this file owns the brain's own
result type and the two mechanics create_agent does not give us for free:
partial steps when the step limit is hit, and tool exceptions fed back to the
model as {'error': ...} instead of escaping the graph.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError

from ..config import Settings
from ..llm.factory import make_chat_model

SYSTEM_PROMPT = """You are Lodestar's assistant — a research companion and coach \
for a personal life dashboard ("your compass for life"). The board \
holds everything in the user's life: work, marriage, family, health, music, \
reading, travel, home.

You can: research and draft answers (web_search + find_related, cite urls), \
operate the board (list/create/update cards), break fuzzy questions into \
concrete sub-questions, and surface connections (find_related returns Leiden \
community ids — same community = same theme; point out likely duplicates).

Board columns: inbox, in-progress, answered. \
Card types: question, problem, task, idea, plan. \
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


class ToolErrorsToJson(AgentMiddleware):
    """A raising tool becomes {'error': str(exc)} fed back to the model.

    create_agent lets tool exceptions escape the graph, so one unreachable
    board would turn into a 500 for the whole chat turn. The hand-rolled loop
    caught them; this restores that in both tool paths — a sync-only
    wrap_tool_call raises NotImplementedError under astream, which is the path
    the route takes.
    """

    @staticmethod
    def _message(request: Any, exc: Exception) -> ToolMessage:
        return ToolMessage(content=json.dumps({'error': str(exc)}),
                           tool_call_id=request.tool_call['id'],
                           name=request.tool_call['name'],
                           status='error')

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        try:
            return handler(request)
        except Exception as exc:
            return self._message(request, exc)

    async def awrap_tool_call(self, request: Any,
                              handler: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            return await handler(request)
        except Exception as exc:
            return self._message(request, exc)


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
        self._graphs: dict[str, Any] = {}
        # Build the default graph now rather than on the first request: an
        # unknown BRAIN_LLM has to fail at boot (create_app builds the agent),
        # which is the no-auto-modes rule. Lazily, a typo would serve a healthy
        # /health and then 500 the first chat.
        self._graph(None)

    def _graph(self, model: str | None):
        """One compiled graph per model name. create_agent binds its model at
        build time, but the Assistant view sends a model per request — so the
        picker needs a graph per pick, cached for the life of the process."""
        key = model or ''
        if key not in self._graphs:
            llm = self._llm or make_chat_model(self.settings, model)
            self._graphs[key] = create_agent(model=llm, tools=self.tools,
                                             system_prompt=self.system_prompt,
                                             middleware=[ToolErrorsToJson()])
        return self._graphs[key]

    @property
    def _config(self) -> dict:
        # A step is a model turn plus a tool turn, and the run ends on a model
        # turn: 2n+1 nodes for n tool calls.
        return {'recursion_limit': 2 * self.max_steps + 1}

    def run(self, messages: list[dict], model: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            # Streamed, not invoked: GraphRecursionError carries no messages,
            # so this is the only way to still report the steps taken.
            for chunk in self._graph(model).stream({'messages': messages},
                                                   config=self._config,
                                                   stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return AgentResult(reply=STEP_LIMIT_REPLY, steps=_steps_from(seen))
        return AgentResult(reply=_reply_from(seen), steps=_steps_from(seen))

    async def arun(self, messages: list[dict],
                   model: str | None = None) -> AgentResult:
        seen: list[BaseMessage] = []
        try:
            async for chunk in self._graph(model).astream({'messages': messages},
                                                          config=self._config,
                                                          stream_mode='values'):
                seen = chunk['messages']
        except GraphRecursionError:
            return AgentResult(reply=STEP_LIMIT_REPLY, steps=_steps_from(seen))
        return AgentResult(reply=_reply_from(seen), steps=_steps_from(seen))
