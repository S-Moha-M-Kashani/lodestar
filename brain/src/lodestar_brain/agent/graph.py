"""The agent: one compiled graph per model the picker can offer.

LangChain owns the loop and the tool schemas. What is ours are the mechanics
`create_agent` does not give for free — a graph cached per provider/model pair,
partial steps when the step limit is hit, and the reconciliation between a
durable thread and a browser that re-sends its whole transcript every turn —
plus the middleware order, which is load-bearing: the fence sits outside the
error handler, so a tool's failure message is fenced too.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage,
                                     RemoveMessage)
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from ..config import Settings
from ..llm import make_chat_model
from ..middleware.cache import ToolResultCache
from ..middleware.errors import ToolErrorMiddleware, _tool_error
from ..middleware.memory import LongTermMemory
from ..middleware.summarize import make_context_editor, make_summarizer
from ..middleware.untrusted import UntrustedToolOutput
from .prompt import SYSTEM_PROMPT
from .result import (STEP_LIMIT_REPLY, AgentResult, _calls_in, _result_from,
                     _steps_from, _text, _usage_from)
from .state import LodestarState, TurnContext
from .trace import TraceCollector, TurnTrace, final_answer

# The graph node that holds the agent's own model call. `create_agent` names it
# this, and `stream_mode='messages'` stamps every message with the node it came
# out of — which is the only thing that tells the assistant's voice apart from a
# model a tool called for its own purposes. Observed against a live turn rather
# than read off a docstring: 'model' for the reply, 'tools' for the relevance
# gate's scores.
MODEL_NODE = 'model'


def _from_model(metadata: Any) -> bool:
    """Did this streamed message come out of the agent's own model call?

    A missing node is *not* trusted. The one thing worse than dropping a token
    is streaming a tool's internal chatter into the transcript, and a stream
    whose metadata has stopped carrying a node is a stream this function can no
    longer make the distinction for.
    """
    return isinstance(metadata, dict) and metadata.get('langgraph_node') == MODEL_NODE


def _spoken(messages: Iterable[BaseMessage]) -> list[tuple[str, str]]:
    """A thread's messages as the browser's transcript would hold them.

    The browser keeps what was *said*: the user's messages and the reply each
    turn ended on. A tool call, a tool answer, and the commentary an AIMessage
    may carry alongside a tool call are all chips in the Assistant rather than
    lines in the transcript, so they are not part of what can be compared.

    A summary is skipped for the same reason, and it is a HumanMessage, which is
    the one that would otherwise be mistaken for something the user typed.
    `SummarizationMiddleware` marks it `lc_source='summarization'` — the
    framework's own flag, read here rather than a prefix match on its wording.
    """
    said: list[tuple[str, str]] = []
    for message in messages:
        if message.additional_kwargs.get('lc_source') == 'summarization':
            continue
        if isinstance(message, HumanMessage):
            said.append(('user', _text(message).strip()))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            if text := _text(message).strip():
                said.append(('assistant', text))
    return said


def _said(message: Any) -> tuple[str, str]:
    """One incoming message as (role, text). Dicts are what the route sends;
    BaseMessage is what an eval or a test may hand `run` directly."""
    if isinstance(message, BaseMessage):
        said = _spoken([message])
        return said[0] if said else ('', '')
    return (str(message.get('role', '')),
            str(message.get('content', '')).strip())


def _hooks(middleware: list, hook: str) -> int:
    """How many of these middlewares become a graph node at `hook`.

    The test `create_agent` itself applies, restated: a hook counts when the
    class overrides either the sync or the async form. Asking the instances
    rather than hard-coding a number means a middleware added later is counted
    without anyone remembering to.
    """
    return sum(1 for m in middleware
               if getattr(m.__class__, hook) is not getattr(AgentMiddleware, hook)
               or getattr(m.__class__, f'a{hook}')
               is not getattr(AgentMiddleware, f'a{hook}'))


def _window(said: list[tuple[str, str]], incoming: list) -> int | None:
    """Where the thread's transcript sits inside the browser's, or None.

    A thread that has never been summarised holds a *prefix* of the browser's
    conversation, and the answer is 0. Once `SummarizationMiddleware` has fired,
    it holds a contiguous *window* instead — the summary stands in for
    everything before it, and what survives verbatim is the tail. Searching for
    the window rather than assuming a prefix is what stops a summarised thread
    being read as a diverged one; without it the very next turn would replace the
    thread wholesale, throwing away the summary that had just been paid for and
    re-sending every message it had replaced.

    Earliest match wins, so an unsummarised thread answers 0 exactly as the
    prefix comparison did. Still exact and still all-or-nothing: a deleted or
    edited turn breaks contiguity and gets no match, which is the wholesale
    replacement it should get.
    """
    for start in range(len(incoming) - len(said) + 1):
        if all(mine == _said(theirs)
               for mine, theirs in zip(said, incoming[start:])):
            return start
    return None


def _turn_input(prior: list[BaseMessage], incoming: list) -> tuple[dict, set]:
    """What of `incoming` a thread that already holds `prior` has not heard.

    The browser re-sends the entire conversation on every turn and cannot do
    otherwise — that is the wire contract. A checkpointed thread already holds
    those messages, and `add_messages` pairs on message id, which the browser's
    messages do not carry: feeding the full list back appends a second copy of
    every turn, and the turn after that a third. The token bill would grow with
    the number of turns squared, which is the opposite of why the thread exists.

    So the incoming list is reconciled against the thread by *content*, aligned
    on role and text and never on position — the same rule `adoptRecordedIds`
    uses in the frontend, for the same reason. When the thread's transcript is
    found in the incoming one, only what follows it is fed. When it is not —
    history edited, a turn deleted, a step-limit reply the thread never wrote —
    the thread's messages are dropped and replaced wholesale, because appending
    onto a history that has moved is how you get a conversation neither side
    ever had.

    Returns the graph input and the ids the thread held *before* this turn, which
    is what lets the caller report this turn's steps and spend rather than the
    thread's whole life.
    """
    said = _spoken(prior)
    start = _window(said, incoming) if len(incoming) >= len(said) else None
    if start is not None:
        return {'messages': incoming[start + len(said):]}, {m.id for m in prior}
    return {'messages': [RemoveMessage(id=REMOVE_ALL_MESSAGES), *incoming]}, set()


class LodestarAgent:
    def __init__(self, *, settings: Settings, tools: list[BaseTool],
                 system_prompt: str = SYSTEM_PROMPT, max_steps: int = 8,
                 llm: BaseChatModel | None = None):
        self.settings = settings
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self._llm = llm            # tests inject a FakeChat; create_app never does
        self._checkpointer = None  # attached by the app's lifespan, not at build
        self._store = None
        self._graphs: dict[tuple[str, str], Any] = {}
        # How many graph nodes one tool round costs. Set by `_middleware`, which
        # is the only thing that knows — see `_run_config`.
        self._per_step = 2
        self._per_run = 0
        # Build the default graph now rather than on the first request: an
        # unknown BRAIN_LLM has to fail at boot (create_app builds the agent),
        # which is the no-auto-modes rule. Lazily, a typo would serve a healthy
        # /health and then 500 the first chat.
        self._graph(None)

    def attach(self, *, checkpointer=None, store=None) -> None:
        """Give the agent its durable state, or take it away again.

        Not a constructor argument, because the two are open connections with a
        lifetime: `create_app` builds the agent at import time and the FastAPI
        lifespan opens the sqlite file when the service actually starts. The
        graph cache is dropped, since a compiled graph binds its checkpointer —
        including the one built in `__init__` purely to validate the backend.

        The offline suites, the evals and any direct caller never attach, and
        that path is unchanged: no thread, no reconciliation, no sqlite.
        """
        self._checkpointer = checkpointer
        self._store = store
        self._graphs.clear()

    def reconfigure(self, settings: Settings) -> None:
        """Swap the agent's settings — a key entered in the Assistant, typically.

        The graph cache is dropped for the same reason `attach` drops it: a
        compiled graph binds its chat model, and the model binds the credential
        it was constructed with, so a kept cache would go on answering with the
        key the brain booted without — configured in the UI, refused on the
        wire, and nothing raises.
        """
        self.settings = settings
        self._graphs.clear()

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
                middleware=self._middleware(llm),
                state_schema=LodestarState, context_schema=TurnContext,
                checkpointer=self._checkpointer, store=self._store)
        return self._graphs[key]

    def _middleware(self, llm: BaseChatModel) -> list:
        """Everything the graph wears, in the one order that is load-bearing.

        The three that wrap a *tool call* nest outside-in as listed, and that
        nesting is the rule:

        - `UntrustedToolOutput` is outermost, so a tool's failure message is
          fenced too — the text in "board unreachable at …" is not ours either.
        - `ToolErrorMiddleware` sits inside it, turning a raising tool into
          something the model can read.
        - `ToolResultCache` is innermost, and that is deliberate: an exception
          reaches it before the error handler has turned it into a value, so it
          simply never stores one. A cache one place further out would remember
          a transient board outage for the rest of the turn.

        The rest touch the *model* call and cannot disturb that: the summariser
        and the tool-call limit are state hooks, the context editor and the
        memory injection wrap the request. Either summarisation knob set to 0
        returns None from its factory and drops out here, so switching it off
        leaves no middleware to ask.
        """
        # The runaway guard `recursion_limit` cannot be. It counts *nodes*, and
        # a model that requests six tool calls in one message spends one node
        # and six calls — six board fetches, or six web searches, against a
        # budget that never noticed. So the limit is per run and generous
        # (parallel calls are legitimate), and 'continue' rather than 'end':
        # the model is told the tool is spent and still gets to answer, which
        # is what the step-limit path had to give up on.
        limit = ToolCallLimitMiddleware(run_limit=2 * self.max_steps,
                                        exit_behavior='continue')
        middleware = [m for m in (UntrustedToolOutput(),
                                  ToolErrorMiddleware(_tool_error),
                                  ToolResultCache(),
                                  make_summarizer(self.settings, llm),
                                  make_context_editor(self.settings),
                                  LongTermMemory(),
                                  limit) if m is not None]
        # `create_agent` compiles each before_model/after_model hook into its own
        # graph node, and `recursion_limit` counts nodes. So a middleware with a
        # state hook silently shortens the step budget — adding two of them here
        # cut a max_steps=2 run to one tool call, which is the sort of change
        # that looks like a model getting worse. `_run_config` derives the limit
        # from this shape instead of from the constant 2n+1 it used to be.
        self._per_step = 2 + _hooks(middleware, 'before_model') \
            + _hooks(middleware, 'after_model')
        self._per_run = _hooks(middleware, 'before_agent') \
            + _hooks(middleware, 'after_agent')
        return middleware

    def _run_config(self, session_id: str | None = None,
                    board_id: str | None = None,
                    trace: TurnTrace | None = None) -> dict:
        # A step is a model turn plus a tool turn, and the run ends on a model
        # turn — which was 2n+1 nodes for n tool calls until middleware started
        # adding nodes of its own. `_per_step` and `_per_run` are that shape,
        # counted from the middleware actually installed, so `max_steps` keeps
        # meaning tool rounds rather than "tool rounds, minus however many state
        # hooks were added since".
        #
        # One thread per chat, so reopening a conversation resumes the state the
        # last turn left. A request that names no session gets a thread of its
        # own, made fresh here: the unnamed batches — the evals, any curl, the
        # tests — share Node's reserved 'adhoc' *record*, and treating that as
        # one conversation would let a script read the last caller's context.
        #
        # `board_id` rides in `configurable` because which board the user is
        # looking at is not the model's decision, and a tool argument is
        # something a model can get wrong or be talked into. Absent means the
        # board API's own default board. The *session* deliberately no longer
        # travels here: it is `TurnContext`, typed and out of the checkpoint.
        #
        # `turn_id` is minted here because here is the one place that runs once
        # per turn: every tool call the model makes while answering one question
        # carries it, and that is what lets `BoardSnapshot` know two reads belong
        # to the same question. A uuid rather than a counter — nothing owns a
        # counter, and a thread that resumes would have to carry one.
        config: dict = {'recursion_limit': self._per_run
                        + self.max_steps * self._per_step
                        + (self._per_step - 1),
                        'configurable': {'thread_id': session_id or
                                         f'adhoc-{uuid4()}',
                                         'turn_id': uuid4().hex}}
        if board_id:
            config['configurable']['board_id'] = board_id
        # The developer trace, when one is asked for. A run callback rather than
        # middleware or a wrapped model: `on_chat_model_start` is fired with the
        # exact list the model is about to be sent, which is the only place that
        # list exists — see agent/trace.py for why every other capture point
        # would be a reconstruction. Absent when nobody is tracing, so a turn
        # that is not being watched carries no handler at all.
        if trace is not None:
            config['callbacks'] = [TraceCollector(trace)]
        return config

    def _run_kwargs(self, session_id: str | None) -> dict:
        """What every one of the three run methods passes alongside the config.

        `durability='async'` writes the checkpoint in the background: resume is
        worth a write, but it is not worth making the user wait for one. Omitted
        rather than passed when nothing is attached — LangGraph warns that it has
        no effect without a checkpointer, and a warning on every offline turn is
        a warning nobody reads.
        """
        kwargs: dict = {'context': TurnContext(session_id=session_id or '')}
        if self._checkpointer is not None:
            kwargs['durability'] = 'async'
        return kwargs

    def _prepare(self, graph, config: dict, messages: list,
                 session_id: str | None) -> tuple[dict, set]:
        """The turn's input, reconciled against the thread if there is one.

        Sync, so it is only ever the sync `run` that calls it — and `run` is for
        callers that attach nothing (the evals, the offline tests). An async
        checkpointer refuses a synchronous read, and loudly, which is the right
        way round: the route is async and always was.
        """
        if self._checkpointer is None:
            return self._whose({'messages': messages}, session_id), set()
        prior = graph.get_state(config).values.get('messages', [])
        payload, before = _turn_input(prior, messages)
        return self._whose(payload, session_id), before

    async def _aprepare(self, graph, config: dict, messages: list,
                        session_id: str | None) -> tuple[dict, set]:
        if self._checkpointer is None:
            return self._whose({'messages': messages}, session_id), set()
        state = await graph.aget_state(config)
        payload, before = _turn_input(state.values.get('messages', []), messages)
        return self._whose(payload, session_id), before

    @staticmethod
    def _whose(payload: dict, session_id: str | None) -> dict:
        """Whose conversation this thread is, recorded in the thread itself.

        The thread id already says it, but a thread id is a key and this is
        state: it is what a middleware reading `LodestarState` can act on
        without reaching into the run config for a string.
        """
        payload['session_id'] = session_id or ''
        return payload

    @staticmethod
    def _fresh(messages: list[BaseMessage], before: set) -> list[BaseMessage]:
        """This turn's messages, out of a state that may hold many turns.

        A resumed thread streams its whole history back, and reading a result off
        that would report the previous turn's tool calls as this turn's steps and
        re-bill its tokens. Filtered by id rather than by count: a wholesale
        replacement reorders nothing but renumbers everything.
        """
        return [m for m in messages if m.id not in before] if before else messages

    @staticmethod
    def _traced(trace: TurnTrace | None, seen: list[BaseMessage],
                interrupted: bool = False) -> None:
        """Close the trace on the way out of a run.

        The envelope was captured by the callback while the turn ran; what is
        left is the message the turn ended on and how it ended. Both are read
        off `seen` rather than off the result, because the result is already a
        projection — and a trace built from a projection is the reconstruction
        this whole surface exists to avoid.
        """
        if trace is None:
            return
        answer = final_answer(seen)
        if interrupted:
            trace.interrupt(answer)
            return
        trace.settle(answer, _usage_from(seen))

    def run(self, messages: list[dict], model: str | None = None,
            provider: str | None = None,
            session_id: str | None = None,
            board_id: str | None = None,
            trace: TurnTrace | None = None) -> AgentResult:
        graph = self._graph(model, provider)
        config = self._run_config(session_id, board_id, trace)
        payload, before = self._prepare(graph, config, messages, session_id)
        seen: list[BaseMessage] = []
        try:
            # Streamed, not invoked: GraphRecursionError carries no messages,
            # so this is the only way to still report the steps taken.
            for chunk in graph.stream(payload, config=config,
                                      stream_mode='values',
                                      **self._run_kwargs(session_id)):
                seen = self._fresh(chunk['messages'], before)
        except GraphRecursionError:
            self._traced(trace, seen, interrupted=True)
            return _result_from(seen, STEP_LIMIT_REPLY)
        result = _result_from(seen)
        self._traced(trace, seen)
        return result

    async def arun(self, messages: list[dict], model: str | None = None,
                   provider: str | None = None,
                   session_id: str | None = None,
                   board_id: str | None = None,
                   trace: TurnTrace | None = None) -> AgentResult:
        graph = self._graph(model, provider)
        config = self._run_config(session_id, board_id, trace)
        payload, before = await self._aprepare(graph, config, messages,
                                               session_id)
        seen: list[BaseMessage] = []
        try:
            async for chunk in graph.astream(payload, config=config,
                                             stream_mode='values',
                                             **self._run_kwargs(session_id)):
                seen = self._fresh(chunk['messages'], before)
        except GraphRecursionError:
            self._traced(trace, seen, interrupted=True)
            return _result_from(seen, STEP_LIMIT_REPLY)
        result = _result_from(seen)
        self._traced(trace, seen)
        return result

    async def astream(self, messages: list[dict], model: str | None = None,
                      provider: str | None = None,
                      session_id: str | None = None,
                      board_id: str | None = None,
                      trace: TurnTrace | None = None
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
        graph = self._graph(model, provider)
        config = self._run_config(session_id, board_id, trace)
        payload, before = await self._aprepare(graph, config, messages,
                                               session_id)
        seen: list[BaseMessage] = []
        sent = 0
        announced: set[str] = set()
        try:
            async for mode, chunk in graph.astream(
                    payload, config=config,
                    stream_mode=['values', 'messages'],
                    **self._run_kwargs(session_id)):
                if mode == 'values':
                    seen = self._fresh(chunk['messages'], before)
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
                # JSON being pasted into the reply as if the model had said it —
                # but an AIMessage is not by itself the *assistant's* voice, and
                # that is the hole this filter had. A tool may call a model of
                # its own, LangChain propagates the run's callbacks into that
                # nested call, and its reply arrives here as an AIMessage like
                # any other. The relevance gate does exactly this: it grades
                # candidates in one call and answers in the shape its own regex
                # reads, so a real turn streamed `1: 9\n2: 0\n3: 1` into the
                # transcript ahead of the answer, looking like debug output the
                # assistant had said. Observed 2026-09-02 on BRAIN_LLM=claude-cli
                # against a 60-card board; invisible to every test, because the
                # e2e runs BRAIN_LLM=fake and the buffered route returns only the
                # final result.
                #
                # So the node decides, not the type. `MODEL_NODE` is where
                # create_agent puts the model call; a nested one runs inside
                # 'tools'. If the framework ever renames the node, tokens stop
                # rather than a tool's chatter starting — the safe direction, and
                # the e2e's streaming checks fail loudly on it.
                message, metadata = chunk
                if (isinstance(message, AIMessage) and _from_model(metadata)
                        and (text := _text(message))):
                    yield 'token', text
        except GraphRecursionError:
            self._traced(trace, seen, interrupted=True)
            yield 'done', _result_from(seen, STEP_LIMIT_REPLY)
            return
        result = _result_from(seen)
        self._traced(trace, seen)
        yield 'done', result


__all__ = ['LodestarAgent']

"""Alternatives considered

## Why did you write your own transcript reconciliation?

Because the two halves of this system disagree about who owns the conversation,
and nothing in the framework arbitrates that. The browser holds the transcript
and re-sends all of it every turn; the checkpointed thread holds the same turns
plus the tool calls the browser never sees. `_turn_input` is the twenty lines
that decide which of the incoming messages the thread has not already heard.

**Why the obvious option fails.** The obvious option is to keep passing the whole
list and let LangGraph sort it out. It cannot: `add_messages` deduplicates on
message id, and messages built from `{'role', 'content'}` dicts are assigned a
fresh uuid on every request. Turn two files a second copy of turn one, turn three
a third, and the token bill grows with the square of the conversation — the exact
cost the thread was added to remove, arriving silently, because nothing raises
and the answers stay plausible. The mirror-image option — trust the thread and
send only the last message — fails on the other side: this board lets a user
delete a single turn, and a thread that never heard about the deletion would keep
answering from a message the user has been shown is gone.

**Why not the framework.** LangChain 1.3 ships middleware for the adjacent
problem, not this one: `SummarizationMiddleware` and `ContextEditingMiddleware`
bound how much context costs, and neither answers "which of these do I already
have". The one piece that does exist is used rather than reimplemented —
`RemoveMessage(REMOVE_ALL_MESSAGES)` is how the divergent case clears the thread,
and `add_messages` still does the appending. What is ours is only the comparison.

**The libraries that would do it.** `difflib.SequenceMatcher` over the two
(role, text) lists would find the longest common prefix and more besides, and is
in the standard library. `jsondiff` or `deepdiff` would diff the structures.
Sending stable ids from the browser — `crypto.randomUUID()` at compose time,
carried in the record and back on every turn — would delete this function
outright and let `add_messages` do exactly what it was designed for; on a
greenfield project that is the design, and it is not a library at all.

**Why they were not adopted, and what would change it.** A fuzzy match is the
wrong tool for a decision that must be exact: a near-alignment on a conversation
is a conversation neither party had, so a prefix comparison that either matches
or gives up is the honest shape, and `SequenceMatcher` would add a similarity
score nobody should act on. Ids at the source are the better design and were
ruled out by scope, not by merit — this change is drop-in, and `ChatBody`,
`server.js` and the frontend transcript do not move. What would settle it is a
count of wholesale replacements in real use: they are supposed to be rare (an
edited or deleted history), and if they are not, the alignment is guessing where
ids would know, and the wire contract should change instead.
"""
