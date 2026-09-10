"""Widget 2: one thread, the board, and nothing that was not argued for.

The second agent behind the Assistant, built beside the first rather than out of
it, so that every capability it ends up with is one somebody carried across on
purpose. It began (v1, 2026-09-08) with exactly two things — the durable thread,
and the reconciliation that keeps a browser re-sending its whole transcript from
doubling that thread's history.

**What it holds now** is that, plus three tools assembled in
`widget2/tools/__init__.py` — `find_cards`, `create_card`, `update_card` — and
the three middlewares those tools make necessary, judged one at a time in
`records/middleware-verdicts.md` and listed in `_middleware` with the
requirement each answers. Four of Widget 1's five were dropped there with
reasons, and none of its other five tools was carried: the owner saw all eight
and took none. Still absent, and absent rather than gated: any way to delete a
card, purge one, or tick a habit.

**What is shared and what is written out here.** `make_chat_model` (all four
engines), `AgentResult`/`AgentStep` and the readers that build them, the two
state schemas, `count_hooks`, the fence, the tool-error handler and the trace
collector are *imported*: they are services with no opinion about which agent
calls them, and sharing the readers is the strongest guarantee that a turn
reports the same facts — and draws the same tool chip — whichever agent
answered. What is written out here is the reconciliation, because it lives in
the module that also assembles Widget 1's middleware stack, and because this
agent's version is genuinely smaller, in one way that is argued below.

**The one deliberate difference from Widget 1's reconciliation: a prefix, not a
window.** Widget 1 searches for the thread's transcript *inside* the browser's,
because `SummarizationMiddleware` rewrites the middle of a thread and leaves a
window rather than a prefix. v1 has no summariser and nothing else that can edit
the thread's past, so a thread here always holds a prefix of the conversation
the browser is re-sending, and `_prefix` is the whole comparison. If stage 3
carries the summariser across, the window search comes with it — that is part of
carrying it, not a gap left here.

Switching `BRAIN_AGENT` mid-chat therefore costs less than it looks, and this
was measured rather than assumed (2026-09-08): a thread Widget 1 wrote, resumed
by v1 and then handed back to Widget 1, kept one copy of every turn in both
directions and never took the wholesale path. The state schema is shared, and
neither agent's reconciliation is confused by an ordinary transcript of the
other's. The one case that does diverge is a thread `SummarizationMiddleware`
has already rewritten — v1 reads that summary as something the user said,
clears the thread and refeeds it from the browser's copy, costing one turn's
resume and no record. The checkpoint file is derived working memory; the chat,
the session and every card live in the board's own databases.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage,
                                     RemoveMessage)
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from langchain_core.tools import BaseTool

from ..agent.result import (STEP_LIMIT_REPLY, AgentResult, _calls_in,
                            _result_from, _steps_from, _text, _usage_from)
from ..agent.state import LodestarState, TurnContext
from ..middleware import PROMPT_RULE, UntrustedToolOutput, count_hooks
from ..middleware.errors import ToolErrorMiddleware, _tool_error
from ..agent.trace import TraceCollector, TurnTrace, final_answer
from ..config import Settings
from ..llm import make_chat_model
from .prompt import SYSTEM_PROMPT

# The graph node holding the agent's own model call — `create_agent` names it
# this, and `stream_mode='messages'` stamps every message with the node it came
# out of. It was written while this agent had no tools, when nothing else could
# call a model and the filter could not yet be wrong — and it is load-bearing
# now that there are three, because a tool may call a model of its own and its
# reply arrives here as an AIMessage like any other. That is how it went wrong
# in Widget 1: the relevance gate grades candidates in one call and answers in
# the shape its own regex reads, so a live turn streamed `1: 9 / 2: 0 / 3: 1`
# into the transcript ahead of the answer (observed 2026-09-02, and invisible
# to every test, because the suite runs `BRAIN_LLM=fake`). None of this agent's
# three tools calls a model today; the filter is what makes that a fact about
# the tools rather than a thing to remember when a fourth arrives.
MODEL_NODE = 'model'


def _from_model(metadata: Any) -> bool:
    """Did this streamed message come out of the agent's own model call?

    A missing node is not trusted: dropping a token beats streaming something
    the assistant did not say.
    """
    return isinstance(metadata, dict) and metadata.get('langgraph_node') == MODEL_NODE


def _spoken(messages: Iterable[BaseMessage]) -> list[tuple[str, str]]:
    """A thread's messages as the browser's transcript would hold them.

    The browser keeps what was *said* — the user's messages, and the reply each
    turn ended on. v1 produces nothing else, so this is a narrower job here than
    in Widget 1: with no tools there are no tool calls to skip and no
    commentary alongside one, and with no summariser there is no summary message
    to recognise. The `isinstance` tests are still written out rather than
    assumed, because a thread resumed from Widget 1 can hold all three.
    """
    said: list[tuple[str, str]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            said.append(('user', _text(message).strip()))
        elif isinstance(message, AIMessage) and not message.tool_calls:
            if text := _text(message).strip():
                said.append(('assistant', text))
    return said


def _said(message: Any) -> tuple[str, str]:
    """One incoming message as (role, text). Dicts are what the route sends;
    a BaseMessage is what a test or an eval may hand `run` directly."""
    if isinstance(message, BaseMessage):
        said = _spoken([message])
        return said[0] if said else ('', '')
    return (str(message.get('role', '')),
            str(message.get('content', '')).strip())


def _prefix(said: list[tuple[str, str]], incoming: list) -> bool:
    """Does the browser's conversation begin with everything the thread holds?

    Exact and all-or-nothing on purpose. A near-alignment on a conversation is a
    conversation neither party had, so this either matches and the suffix is fed,
    or it does not and the thread is replaced. An edited or deleted turn breaks
    it, which is the wholesale replacement such a turn should get.
    """
    return len(incoming) >= len(said) and all(
        mine == _said(theirs) for mine, theirs in zip(said, incoming))


def _turn_input(prior: list[BaseMessage], incoming: list) -> tuple[dict, set]:
    """What of `incoming` a thread that already holds `prior` has not heard.

    The browser re-sends the entire conversation every turn and cannot do
    otherwise — that is the wire contract. The thread already holds those turns,
    and `add_messages` deduplicates on message id, which messages built from
    `{'role', 'content'}` dicts do not carry: feeding the whole list back files
    a second copy of every turn, and the turn after that a third, so the bill
    grows with the square of the conversation while nothing raises and the
    answers stay plausible.

    Returns the graph input and the ids the thread held *before* this turn,
    which is what lets the caller report this turn's spend rather than the
    thread's whole life.
    """
    said = _spoken(prior)
    if _prefix(said, incoming):
        return {'messages': incoming[len(said):]}, {m.id for m in prior}
    return {'messages': [RemoveMessage(id=REMOVE_ALL_MESSAGES), *incoming]}, set()


class Widget2Agent:
    """The second agent. Same call shape as `LodestarAgent`, far less inside it.

    The methods the HTTP route uses — `settings`, `attach`, `reconfigure`,
    `arun`, `astream` — match Widget 1's, because the route must not know which
    agent it is talking to. `run` exists for the callers that attach nothing:
    the evals, and the offline tests.
    """

    def __init__(self, *, settings: Settings,
                 tools: list[BaseTool] | None = None, max_steps: int = 8,
                 system_prompt: str = SYSTEM_PROMPT,
                 llm: BaseChatModel | None = None):
        self.settings = settings
        #: What this agent can do beyond conversing, and `/health` reports it —
        #: read off this list, so an absent capability is visible without
        #: reading any source. A real empty list rather than None when there are
        #: none, so the readers of both agents are the same code.
        self.tools = list(tools or [])
        #: Tool *rounds*, not graph nodes. Meaningless with no tools and
        #: load-bearing with them: see `_middleware` for what one round costs.
        self.max_steps = max_steps
        # The fence's own rule travels with the fence, and only when the fence
        # is installed. Widget 1 concatenates it into the prompt constant, which
        # is fine for an agent that always has tools; this agent may have none,
        # and a prompt describing delimiters around results that never arrive
        # would be a rule about nothing. `prompt.py` therefore holds the coach
        # alone and the graph appends the rest — so the prompt cannot name a
        # marker the wrapper does not write.
        self.system_prompt = (system_prompt + PROMPT_RULE if self.tools
                              else system_prompt)
        self._llm = llm            # tests inject a FakeChat; create_app never does
        self._checkpointer = None  # attached by the app's lifespan, not at build
        self._graphs: dict[tuple[str, str], Any] = {}
        # How many graph nodes one tool round costs, and what a run costs
        # outside its rounds. Set by `_middleware`, which is the only thing that
        # knows what is installed.
        self._per_step, self._per_run = 2, 0
        # Built now rather than on the first request, the same rule Widget 1
        # follows: an unknown BRAIN_LLM has to fail at boot, because lazily a
        # typo serves a healthy /health and then 500s the first question.
        self._graph(None)

    def attach(self, *, checkpointer=None, store=None) -> None:
        """Give this agent its durable state, or take it away again.

        Not a constructor argument: it is a live sqlite connection owned by the
        FastAPI lifespan, and the graph cache is dropped because a compiled
        graph binds its checkpointer.

        `store` is accepted and not used. The long-term store is what a memory
        tool writes to and v1 has none — but the lifespan opens both and hands
        both to whichever agent it built, and a selector that had to remember
        which agent takes which half is a selector that will eventually forget.

        **Both agents share this file, and that was measured rather than hoped
        for (2026-09-08).** A thread Widget 1 wrote, resumed by v1 and then
        handed back, kept one copy of every turn in both directions and never
        took the wholesale path — because the state schema is the same and
        neither agent's reconciliation is confused by the other's transcript.
        The one case that still diverges is a thread `SummarizationMiddleware`
        has rewritten: v1 reads that summary as something the user said, clears
        the thread and refeeds it from the browser's copy, costing one turn's
        resume and no record.
        """
        self._checkpointer = checkpointer
        self._graphs.clear()

    def reconfigure(self, settings: Settings) -> None:
        """Swap the settings — a key entered in the Assistant, typically.

        The cache is dropped because a compiled graph binds its chat model, and
        the model binds the credential it was built with: a kept graph would go
        on answering with the key the brain booted without.
        """
        self.settings = settings
        self._graphs.clear()

    def _middleware(self) -> list:
        """Everything this graph wears, and why each piece is on it.

        **Three, and each answers a requirement this agent now has** rather than
        one Widget 1 had. `records/middleware-verdicts.md` holds the judgement
        that dropped the other four.

        - `UntrustedToolOutput` is **outermost**, so a tool's own failure
          message is fenced too — the text in "board unreachable at …" is not
          ours either. It is here because `find_cards` is the first thing to put
          the owner's own card notes in front of the model, and that is exactly
          the channel this project's injection run actually failed in: 3 of 12
          on `openai/gpt-5-nano` (2026-08-13), every web payload refused, all
          three failures in card notes.
        - `ToolErrorMiddleware` sits inside it. All three tools reach the board
          over HTTP, and `BoardClient` raises for status: without this, a board
          that is down, a 401 from an unset token, a 400 from `/api/proposals`
          or a read timeout does not merely lose the answer — the stream dies
          before its `done` frame, and the route records a turn only on `done`,
          so the question and everything said before it never reach
          `assistant.db`. On a board whose first pillar is *never lose a
          thought*, that is the failure, not the 500.
        - `ToolCallLimitMiddleware` is the runaway guard, and it is the one
          `recursion_limit` cannot be: a model that requests six tool calls in
          one message spends one node and six calls — six board reads against a
          budget that never noticed. `exit_behavior='continue'`, so the model is
          told the tool is spent and still gets to answer.

        **With no tools there is no stack at all.** Not an optimisation: an
        agent that fenced nothing while claiming a fence, or spent nodes on hooks
        for tools it does not hold, would be describing itself wrongly. This is
        the same rule the prompt follows one line up.
        """
        if not self.tools:
            self._per_step, self._per_run = 2, 0
            return []
        middleware = [UntrustedToolOutput(),
                      ToolErrorMiddleware(_tool_error),
                      ToolCallLimitMiddleware(run_limit=2 * self.max_steps,
                                              exit_behavior='continue')]
        # A step is a model turn plus a tool turn, and every middleware hook is
        # its own graph node — so a hook added here silently shortens the tool
        # budget unless the limit is derived from what is actually installed.
        # `count_hooks` is shared with Widget 1 for that reason: two copies of
        # this arithmetic is how the two agents would come to disagree about
        # what a step costs.
        self._per_step = 2 + count_hooks(middleware, 'before_model') \
            + count_hooks(middleware, 'after_model')
        self._per_run = count_hooks(middleware, 'before_agent') \
            + count_hooks(middleware, 'after_agent')
        return middleware

    def _graph(self, model: str | None, provider: str | None = None):
        """One compiled graph per provider/model pair.

        `create_agent` binds its model at build time and the picker can move
        between engines per request, so the provider is part of the key: a model
        slug alone is not a complete destination. Every engine this service
        offers is reached by calling `make_chat_model` and this agent keeps no
        list of its own — which is what makes an engine added once available to
        both agents.
        """
        key = (provider or self.settings.llm_provider, model or '')
        if key not in self._graphs:
            llm = self._llm or make_chat_model(self.settings, model, provider)
            middleware = self._middleware()
            # The refusal the spec asks for, at the one place a graph is built:
            # an agent that has gained a returning tool must not serve a turn
            # without the fence. Checked against what `_middleware` actually
            # returned rather than trusted, because the failure this guards is a
            # later edit to that method — and it fails at boot, since
            # `__init__` builds the default graph, rather than on the first
            # question somebody asks.
            if self.tools and not any(isinstance(m, UntrustedToolOutput)
                                      for m in middleware):
                raise ValueError(
                    'widget2 holds tools but no untrusted-output fence; '
                    'every tool result must reach the model as fenced data')
            self._graphs[key] = create_agent(
                model=llm, tools=self.tools, system_prompt=self.system_prompt,
                middleware=middleware,
                # `state_schema` is Widget 1's, deliberately: both agents write
                # one thread shape into the one checkpoint file, which is what
                # makes switching `BRAIN_AGENT` mid-chat cost nothing (measured
                # 2026-09-08 — see `attach`). `context_schema` is Widget 1's for
                # a sharper reason: it is what the framework strips from a
                # *tool's* schema, so the session reaches a tool without the
                # model being able to name or spoof it. v1 passed none because
                # it had no tool to strip it from.
                state_schema=LodestarState, context_schema=TurnContext,
                checkpointer=self._checkpointer)
        return self._graphs[key]

    def _run_config(self, session_id: str | None = None,
                    board_id: str | None = None,
                    trace: TurnTrace | None = None) -> dict:
        """The run's configuration: which thread, which board, who is watching.

        **`recursion_limit` is not optional and its absence was not harmless.**
        v1 set none on the ground that one model node needs no bound, and the
        installed framework's default is `DEFAULT_RECURSION_LIMIT = 10007` — so
        the day this agent gained a board tool, "no limit" meant on the order of
        five thousand tool rounds against the owner's board before anything
        stopped it. It is derived from the middleware actually installed
        (`_middleware`), never hard-coded, so `max_steps` keeps meaning tool
        rounds after a hook is added rather than silently meaning fewer.

        **No `turn_id`, and that is now a decision rather than an absence.** It
        exists to let several board-reading tools share one `BoardSnapshot`
        fetch. This agent has one reader, and its writers read nothing — so
        there is no second fetch for a snapshot to collapse, and a snapshot
        would buy a turn-long staleness window for no saving. It comes back the
        day a second tool reads the board.

        `board_id` rides in `configurable` because which board the owner is
        looking at is not the model's decision, and a tool argument is something
        a model can get wrong or be talked into. That is also where every
        existing consumer reads it — `board_of`, and Widget 1's tools — so
        `find_cards` needs no reader of its own. It was accepted and dropped in
        v1, correctly: an unread key here is a fact checkpointed for nobody, and
        v1 had no tool that could read a board.

        One thread per chat, so reopening a conversation resumes where it was
        left. A request naming no session gets a thread of its own, minted here:
        the unnamed callers — the evals, a curl, the tests — share the board's
        reserved `adhoc` *record*, and treating that as one conversation would
        let a script read the last caller's context.
        """
        config: dict = {'recursion_limit': self._per_run
                        + self.max_steps * self._per_step
                        + (self._per_step - 1),
                        'configurable': {'thread_id':
                                         session_id or f'adhoc-{uuid4()}'}}
        if board_id:
            config['configurable']['board_id'] = board_id
        # A run callback rather than middleware or a wrapped model:
        # `on_chat_model_start` is fired with the exact list the model is about
        # to be sent, and that list exists nowhere else. Absent when nobody is
        # tracing, so an unwatched turn carries no handler at all.
        if trace is not None:
            config['callbacks'] = [TraceCollector(trace)]
        return config

    def _run_kwargs(self, session_id: str | None) -> dict:
        """What all three run methods pass alongside the config.

        **`context` is what makes `context_schema` true.** `_graph` declared
        the schema and nothing ever passed a value, so `runtime.context` read
        `None` where Widget 1's tools read `TurnContext(session_id=…)` — a
        half-wired mechanism whose own comment claimed it was whole, found by
        stage 4's parity record rather than by a failing turn, because none of
        the three tools reads it yet. Populated rather than declared away: this
        is the framework's channel for a fact the model must not be able to
        name, and the next tool needing it must find it wired.

        `durability='async'` writes the checkpoint in the background: resume is
        worth a write, not worth waiting for. Omitted rather than passed with
        nothing attached, because LangGraph warns that it has no effect without
        a checkpointer and a warning on every offline turn is one nobody reads.
        """
        kwargs: dict = {'context': TurnContext(session_id=session_id or '')}
        if self._checkpointer is not None:
            kwargs['durability'] = 'async'
        return kwargs

    def _prepare(self, graph, config: dict, messages: list,
                 session_id: str | None) -> tuple[dict, set]:
        """The turn's input, reconciled against the thread if there is one.

        Sync, so only the sync `run` calls it — and `run` is for callers that
        attach nothing. An async checkpointer refuses a synchronous read, loudly,
        which is the right way round: the route is async and always was.
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

        Kept for a concrete current reason rather than for a future reader:
        Widget 1 writes this channel, both agents share one checkpoint file, and
        a thread whose shape depended on which agent last wrote it is a thread
        neither can be trusted to resume. Measured — see `attach`.
        """
        payload['session_id'] = session_id or ''
        return payload

    @staticmethod
    def _fresh(messages: list[BaseMessage], before: set) -> list[BaseMessage]:
        """This turn's messages, out of a state that may hold many turns.

        A resumed thread streams its whole history back on `stream_mode='values'`
        — that is how the installed LangGraph streams, not a Widget 1 quirk — and
        reading a result off it would re-bill the previous turn's tokens.
        Filtered by id and never by count: a wholesale replacement reorders
        nothing but renumbers everything.
        """
        return [m for m in messages if m.id not in before] if before else messages

    @staticmethod
    def _traced(trace: TurnTrace | None, seen: list[BaseMessage],
                interrupted: bool = False) -> None:
        """Close the trace on the way out of a run.

        The envelope was captured by the callback while the turn ran; what is
        left is the message it ended on and how it ended. Both read off `seen`
        rather than off the result, because the result is already a projection
        and a trace built from one is the reconstruction this surface exists to
        avoid.
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
            # Streamed rather than invoked, so a run that hits a limit can still
            # report what it did: GraphRecursionError carries no messages.
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
        ('done', AgentResult) last.

        'calling' exists because a requested-but-unanswered call is deliberately
        not a step: a board read that takes a second would otherwise emit
        nothing at all for the part of the turn the reader is waiting through,
        and the Assistant would show a timer ticking with nothing beside it. It
        carries no result because there is none yet; the matching 'step'
        follows, in request order.

        Both are read through `agent/result.py`'s own readers rather than
        rebuilt here — the same functions Widget 1 streams from, which is what
        guarantees a tool chip means the same thing whichever agent drew it.
        With no tools the two branches simply never fire, as they did for v1.

        Tokens are provisional, and a consumer must replace what it accumulated
        with `done.reply`: text can arrive on an AIMessage that also requests a
        tool (commentary before the work, not the answer), and the step-limit
        path abandons the transcript entirely for STEP_LIMIT_REPLY.
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


__all__ = ['Widget2Agent']
