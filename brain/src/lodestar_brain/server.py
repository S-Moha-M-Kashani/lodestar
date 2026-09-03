"""FastAPI wiring. create_app() is the composition root: every swappable
module (LLM provider, search provider, embedder) is chosen here from Settings."""
import base64
import binascii
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.sqlite import AsyncSqliteStore
from pydantic import BaseModel, Field

from .agent import AgentResult, AgentStep, LodestarAgent
from .agent.trace import TurnTrace
from .board import BoardClient, BoardSnapshot
from .config import Settings, load_settings
from .llm import make_chat_model, served_models
from .middleware import configure_tracing
from .pricing import model_prices, turn_cost
from .safety import make_url_safety
from .retrieval import (CardIndex, ChatStore, coverage, expand_queries,
                        gate_llm, make_embeddings, make_reranker)
from .topics import detect_drift
from .tools.board import make_board_tools
from .tools.memory import make_memory_tool
from .tools.recap import make_recap_tool
from .tools.retrieve import make_recall_tool, make_retrieve_tool
from .tools.websearch import DdgsSearch, make_search_tool
from .voice import make_transcriber
from .voice.base import TranscriptionError

# Two different events, kept apart because the frontend reacts differently:
# `mutated` means the board changed and the client should adopt server state,
# `proposed` means a card is waiting for the user's approval and only the
# proposals list needs refreshing. Creating a card no longer changes the board.
# Nothing mutates the board any more: create_card proposes a card and
# update_card suggests a change, and both wait for the user. So the browser is
# only ever told to refresh its suggestion lists, never to adopt server state
# mid-conversation — there is no server-side change to adopt.
MUTATING_TOOLS: set[str] = set()
PROPOSING_TOOLS = {'create_card', 'update_card'}

# How much conversation one turn may carry. The browser sends the whole history
# on every turn, so a chat that runs long is a bigger request each time, and
# there is nothing above this that counts messages — Node's 5 MB body guard is
# about bytes on the wire, not about what a context window costs.
#
# Two caps, because there are two ways to arrive with too much: a thousand
# one-word messages and one novel-length message both overrun a context, and a
# character cap alone lets the first through. MAX_CHARS is roughly 30k tokens of
# English, which is generous for a remote model and already past what a small
# local one will hold.
#
# Deliberately a refusal and not a silent trim of the oldest messages: dropping
# the start of a conversation is the kind of quiet loss this project does not do.
# The user is told to start a new chat, and keeps the old one on screen.
MAX_MESSAGES = 80
MAX_CHARS = 120_000


class ChatBody(BaseModel):
    messages: list[dict]
    model: str | None = None
    # The browser defaults to the local Ollama provider. OpenRouter is an
    # explicit alternative because it can use billed remote models such as Nano,
    # and the two -cli backends answer on this machine's own subscriptions.
    #
    # Spelled out rather than derived from `llm.UI_PROVIDERS`: pydantic needs a
    # static Literal to validate against, and a set comprehension here would
    # trade a wire contract you can read for one you have to run. The two must
    # agree — a provider the picker offers and this rejects is a 422 the browser
    # can do nothing with, which is what the -cli backends were until now.
    provider: Literal['ollama', 'openrouter', 'claude-cli',
                      'codex-cli'] | None = None
    # Which chat this turn belongs to. Optional rather than required: the evals,
    # any curl and sixteen tests post without one, and none of those should be a
    # lost turn — Node files an unnamed batch under its reserved 'adhoc' chat.
    # Empty is omitted from the record rather than sent as '', so the server can
    # tell "no session named" from "a session named the empty string".
    session_id: str = ''
    # Which board the user is looking at. Optional and omitted-when-empty for
    # exactly the same reasons, and it decides which cards the tools read and
    # which board a proposal lands on — so an Assistant answering about one
    # board can never file its suggestions on another.
    board_id: str = ''


class TopicCheckBody(BaseModel):
    """Asked before a turn is sent, never after. `recent` is the session's
    recent user messages; `text` is what is in the composer."""

    recent: list[str]
    text: str


class TranscribeBody(BaseModel):
    audio: str            # base64; the browser encodes 16 kHz mono WAV
    format: str = 'wav'
    model: str | None = None


class KeyBody(BaseModel):
    key: str


class RecallBody(BaseModel):
    text: str
    # Bounded exactly as RecallChatArgs' k already was. Unbounded, one request
    # reads out the whole collection — and this route is reachable straight from
    # the browser, where the tool is only reachable through the model.
    k: int = Field(5, ge=1, le=20)


def _refuse_if_oversized(messages: list[dict]) -> None:
    """413 for a conversation past the caps. Called by every chat route."""
    if len(messages) > MAX_MESSAGES:
        raise HTTPException(413, f'this conversation carries more than '
                                 f'{MAX_MESSAGES} messages — start a new chat')
    total = sum(len(str(m.get('content', ''))) for m in messages)
    if total > MAX_CHARS:
        raise HTTPException(413, f'this conversation carries more than '
                                 f'{MAX_CHARS} characters — start a new chat')


def _step_json(step: AgentStep) -> dict:
    """What the Assistant shows of one tool call: the name, what it was asked,
    and what it answered. `result` is what turns a bare chip into evidence."""
    return {'tool': step.tool, 'arguments': step.arguments, 'result': step.result}


def _turn_json(result: AgentResult, cost: float | None = None) -> dict:
    """The whole turn. Built here so the buffered route and the stream's `done`
    event cannot drift into reporting the same turn differently."""
    return {'reply': result.reply,
            'mutated': any(s.tool in MUTATING_TOOLS for s in result.steps),
            'proposed': any(s.tool in PROPOSING_TOOLS for s in result.steps),
            'steps': [_step_json(s) for s in result.steps],
            # null when the model reported nothing, so the Assistant can stay
            # silent rather than claim a turn cost zero.
            'usage': result.usage,
            # USD, unrounded — how many decimals to show is the reader's
            # question. null when the price is not known, which the Assistant
            # renders as no figure at all rather than as free. See pricing.py.
            'cost': cost}


def _sse(event: str, data: dict) -> str:
    # json.dumps escapes newlines, so no payload can end a frame early.
    return f'event: {event}\ndata: {json.dumps(data)}\n\n'


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    # Egress-affecting and sync, so it runs before anything that could emit a
    # trace — not in the async lifespan. 'off' must actually call langsmith's
    # configure(enabled=False): a stale LANGCHAIN_TRACING_V2=true in the shell
    # otherwise keeps shipping traces (middleware/tracing.py).
    configure_tracing(settings)

    board = BoardClient(settings.board_api_url,
                        token=settings.board_api_token)
    # One snapshot behind all three board-reading tools, which is what makes a
    # turn that reaches for three of them fetch `/api/state` once. The routes
    # below deliberately keep the bare client: they are not a turn, and nothing
    # bounds how stale an answer of theirs would be allowed to get.
    snapshot = BoardSnapshot(board)
    embeddings = make_embeddings(settings.embedder, settings,
                                 settings.embed_model)
    # Built here and not inside the index, like the url-safety checker: an
    # unknown BRAIN_RERANKER, or a hosted one with no key, must stop the boot
    # rather than be discovered on somebody's first question.
    index = CardIndex(embeddings, rerank=make_reranker(settings.reranker,
                                                       settings,
                                                       settings.rerank_model))
    # One chat model serves the gate and the recap summary — the same model
    # that answers, so neither needs a model of its own. `gate_llm` is where
    # BRAIN_GRADER is validated: an unknown value raises at boot rather than
    # leaving the gate quietly switched off.
    chat_model = make_chat_model(settings)
    grader = gate_llm(settings.grader, chat_model)
    memory = None
    if settings.chroma_url:
        try:
            memory = ChatStore(settings.chroma_url, embeddings,
                               collection=settings.chat_collection,
                               database=settings.chroma_database)
        except Exception as exc:
            # Chroma is optional infrastructure: the agent, board tools, web
            # search and card retrieval all work without it. Taking the whole
            # brain down because a container is stopped would be the worse
            # failure — so log loudly and serve on with recall unavailable.
            memory = None
            logging.getLogger(__name__).warning(
                'chat memory disabled: Chroma at %s is unreachable (%s)',
                settings.chroma_url, exc)
    tools = [*make_board_tools(snapshot),
             # Built here, not inside the tool: an unconfigured checker must stop
             # the boot, not be discovered on the first search.
             make_search_tool(DdgsSearch(),
                              safety=make_url_safety(settings.url_safety, settings)),
             # find_related is the agent's one search over everything the user
             # has: the board, and — when Chroma is up — the chat record too.
             make_retrieve_tool(index, snapshot, llm=grader,
                                threshold=settings.grade_threshold,
                                memory=memory),
             # daily_recap answers "what were my concerns?" from the records —
             # a missing Chroma costs it the chunk count, never the recap.
             make_recap_tool(snapshot, store=memory, llm=chat_model),
             # The agent's own notes across conversations. It needs no client:
             # the store is attached to the graph by the lifespan and reaches the
             # tool through ToolRuntime, so a brain with no durable state simply
             # has a tool that says so.
             make_memory_tool()]
    if memory is not None:
        tools.append(make_recall_tool(memory))
    async def sync_chat_index() -> None:
        """Rebuild what the derived Chroma index missed while it was down.

        Chroma is derived from assistant.db, so a turn recorded while it was
        stopped is indexed here — and a chat deleted meanwhile is pruned here,
        because `sync` only ever adds and a deleted conversation resurfacing in
        an answer is the worst version of this feature.

        Best-effort: compose starts the board and the brain in no promised
        order, so a board that is not up yet is logged and never fatal.

        In the lifespan rather than in `create_app`'s body, which is where it
        used to be, because the board client is a coroutine now and
        `create_app` is not. It cannot become one either: `uvicorn
        lodestar_brain.server:app` imports the module — and therefore builds the
        app — from inside `Server.serve()`, which is already running an event
        loop, so an `asyncio.run` here would raise at boot in production and
        nowhere else. The lifespan is the honest home anyway: this is something
        the *service* does when it starts, not something the object graph is.
        """
        if memory is None:
            return
        try:
            recorded = await board.list_all_chat()
            added, dropped = await memory.areconcile(recorded)
            if added or dropped:
                logging.getLogger(__name__).info(
                    'chat index: %d message(s) indexed, %d pruned at boot',
                    added, dropped)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                'chat index not synced from the record: %s', exc)

    agent = LodestarAgent(settings=settings, tools=tools,
                          max_steps=settings.max_agent_steps)
    transcriber = make_transcriber(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Open the agent's durable state for as long as the service runs.

        Here rather than in `create_app`'s body because both are live sqlite
        connections: they belong to the *running* service, not to the object
        graph, and a connection opened at import time is one nothing closes.
        The checkpointer (one thread per chat) and the long-term store share a
        single file — one place to look, one file to delete. The chat index's
        boot sync happens here too, for a reason of its own: see
        `sync_chat_index`.

        `assistant.db` and `board.db` are not touched. This file is derived
        working memory: losing it costs the agent its resume, never a card and
        never a recorded turn, which is why no backup covers it.

        A test or an eval builds `Settings` directly and gets `:memory:`; only a
        brain booted from the environment writes to disk.
        """
        path = settings.checkpoint_db
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(path) as checkpointer, \
                AsyncSqliteStore.from_conn_string(path) as store:
            await checkpointer.setup()
            await store.setup()
            agent.attach(checkpointer=checkpointer, store=store)
            await sync_chat_index()
            try:
                yield
            finally:
                # Dropped before the connections close, so a late request
                # cannot reach a checkpointer that is already shut.
                agent.attach()

    app = FastAPI(title='lodestar-brain', lifespan=lifespan)

    @app.get('/health')
    def health() -> dict:
        return {'ok': True, 'service': 'lodestar-brain'}

    @app.get('/agent/models')
    def models() -> dict:
        """Which models this brain can serve, so the picker cannot offer one it
        cannot load. Under /agent/ because that is the prefix the board proxies.

        Only a local backend answers with a list: see `served_models`."""
        return served_models(settings)

    # What the brain booted with. An empty save restores this rather than '',
    # so a stack keyed by env cannot be un-configured from the browser.
    boot_key = settings.openrouter_api_key

    @app.get('/agent/key')
    def key_status() -> dict:
        """Whether a hosted-API key is in force — never the key itself."""
        return {'configured': bool(agent.settings.openrouter_api_key)}

    @app.post('/agent/key')
    def set_key(body: KeyBody) -> dict:
        """Take an OpenRouter key typed into the Assistant's settings drawer.

        Write-only by design: both routes answer yes or no and no response ever
        carries the key. The agent is handed whole new settings and drops its
        graph cache — a compiled graph binds the credential its model was
        constructed with, so anything less keeps answering with the old one.
        """
        key = body.key.strip() or boot_key
        agent.reconfigure(replace(agent.settings, openrouter_api_key=key))
        return {'configured': bool(key)}

    def priced(result: AgentResult, body: ChatBody) -> float | None:
        """What this turn cost, in USD, or None if that is not knowable.

        Priced against the model and provider the *request* named, not the ones
        the brain booted with: the picker can move between models mid-conversation
        and a turn's price is the price of whatever served it. Shared by both chat
        routes for the same reason `_turn_json` is — two routes pricing one turn
        differently is a bug nobody would find.
        """
        settings_for_turn = settings
        if body.provider and body.provider != settings.llm_provider:
            settings_for_turn = replace(settings, llm_provider=body.provider)
        return turn_cost(result.usage, model_prices(settings_for_turn, body.model))

    async def remember(body: ChatBody, result: AgentResult,
                       cost: float | None) -> None:
        """Record both sides of the exchange — durably first (assistant.db,
        through the Node API like every write), then into the derived Chroma
        index. Every chat route must call this: a second route is a second
        place to forget. Failures are logged, never raised — the reply the
        user is already reading must not become a 500 after the fact, and a
        turn the record missed is still picked up by the next boot's sync
        only if it was recorded, so the log line is the whole trace.

        **Both routes run this after the response, as a background task.** It is
        one HTTP round trip to Node plus an embedding pass over the turn, and
        none of it is anything the user is waiting to be told — they are reading
        the reply while it happens. "Never raised" is what makes that safe to
        move: a background task has no status code left to fail with, so a
        failure that could reach the caller would arrive as a broken connection
        after a delivered answer. This one cannot.

        The assistant row carries the turn's receipt: its tool steps, so a
        reopened chat shows the evidence and not only the prose, and its usage
        and price. The brain is the only place all three are known at once, and
        it is already the only writer of a turn.
        """
        last_user = next((m.get('content', '') for m in reversed(body.messages)
                          if m.get('role') == 'user'), '')
        turn: list[dict] = []
        if last_user.strip():          # the record refuses empty rows
            turn.append({'role': 'user', 'content': last_user})
        if result.reply.strip():
            turn.append({'role': 'assistant', 'content': result.reply,
                         'steps': [_step_json(s) for s in result.steps],
                         'usage': result.usage,
                         # None, never 0.0, when the price is unknown: see
                         # pricing.py. The column is nullable for this reason.
                         'cost': cost})
        if not turn:
            return
        try:
            rows = await board.record_chat(turn, session_id=body.session_id,
                                           board_id=body.board_id)
        except Exception:
            logging.getLogger(__name__).exception(
                'chat record unreachable — this turn is NOT in assistant.db')
            return
        if memory is None:
            return
        try:
            await memory.aindex_messages(rows)
        except Exception:
            # Recorded but not indexed: the next boot's sync rebuilds this.
            logging.getLogger(__name__).exception('chat index write failed')

    def tracing_turn(body: ChatBody) -> TurnTrace | None:
        """A trace for this turn, or None when nobody is tracing.

        Off is the default and the common case, and it costs exactly nothing:
        no handler is attached to the run, and no request is made. `BRAIN_TRACE`
        is the switch, and the page that reads what it files is gated separately
        on the board — capture and viewing are two risks in two services.
        """
        if settings.trace != 'board':
            return None
        return TurnTrace(session_id=body.session_id, board_id=body.board_id,
                         model=body.model or settings.model,
                         provider=body.provider or settings.llm_provider)

    async def file_trace(trace: TurnTrace | None) -> None:
        """Send the record to the board. Never raises, the `remember` rule: a
        debugging aid that can 500 the turn it is describing is worse than no
        debugging aid, and this one runs twice per turn."""
        if trace is None:
            return
        try:
            await board.record_trace(trace.as_dict())
        except Exception:
            logging.getLogger(__name__).exception(
                'trace not filed — the turn itself is unaffected')

    @app.post('/agent/chat')
    async def chat(body: ChatBody, background: BackgroundTasks) -> dict:
        # Async because cycle 2's MCP tools are coroutine-only. Safe today: the
        # sync tools (board HTTP, ddgs, Chroma) run in LangChain's thread
        # executor, so nothing here blocks the event loop.
        _refuse_if_oversized(body.messages)
        trace = tracing_turn(body)
        await file_trace(trace)          # in flight, before anything is known
        try:
            result = await agent.arun(body.messages, model=body.model,
                                      provider=body.provider,
                                      session_id=body.session_id,
                                      board_id=body.board_id,
                                      trace=trace)
        except Exception as exc:
            if trace is not None:
                trace.fail(str(exc))
                await file_trace(trace)
            raise
        cost = priced(result, body)
        # After the answer is sent, not before it. Starlette awaits this once
        # the body is on the wire, so the record is written while the user is
        # already reading — the turn still always lands, it just stops being
        # something they wait for.
        background.add_task(remember, body, result, cost)
        background.add_task(file_trace, trace)
        return _turn_json(result, cost)

    @app.post('/agent/chat/stream')
    async def chat_stream(body: ChatBody) -> StreamingResponse:
        """The same turn as /agent/chat, reported as it happens.

        Kept as a second route rather than a mode of the first: the buffered one
        is what the evals and any non-browser caller want, and a route that
        answers with two different content types depending on a flag is worse
        than two routes.
        """
        # Before the StreamingResponse exists, so an over-long conversation is a
        # 413 the browser can read. Raised from inside the generator it would be
        # a 200 that dies mid-stream.
        _refuse_if_oversized(body.messages)
        # What the turn ended on, filled in by the generator and read by the
        # background task once the last frame is sent. A list rather than a
        # flag: a stream that died before `done` has no turn to record, which is
        # the same thing as before — a reply that was never delivered.
        finished: list[tuple[AgentResult, float | None]] = []
        # Filed before the first frame and again once the turn settles, so a
        # turn that hangs is inspectable while it hangs — the case a record
        # written only at the end cannot show at all.
        trace = tracing_turn(body)

        async def events():
            await file_trace(trace)
            try:
                async for kind, payload in agent.astream(
                        body.messages, model=body.model, provider=body.provider,
                        session_id=body.session_id, board_id=body.board_id,
                        trace=trace):
                    if kind == 'calling':
                        yield _sse('calling', payload)   # already {tool, arguments}
                    elif kind == 'step':
                        yield _sse('step', _step_json(payload))
                    elif kind == 'token':
                        yield _sse('token', {'text': payload})
                    else:
                        cost = priced(payload, body)
                        finished.append((payload, cost))
                        yield _sse('done', _turn_json(payload, cost))
            except Exception as exc:
                # The headers left long ago, so there is no status code to fail
                # with. Staying quiet would leave the browser on "Thinking…"
                # forever — the hang this route exists to remove.
                logging.getLogger(__name__).exception('chat stream failed')
                if trace is not None:
                    trace.fail(str(exc))
                yield _sse('error', {'message': str(exc)})

        async def record() -> None:
            """The turn, recorded once the browser has the whole stream.

            Here rather than beside the `done` frame it belongs to: recording
            there held the last event back for a Node round trip and an
            embedding pass, so the answer finished arriving after the work of
            filing it. Starlette awaits this after the final chunk.
            """
            for result, cost in finished:
                await remember(body, result, cost)
            # Filed whatever happened: a turn that failed is exactly the one
            # somebody is about to go looking for.
            await file_trace(trace)

        # no-cache and no buffering: an intermediary holding the frames back
        # would deliver a correct transcript and none of the progress.
        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache',
                                          'X-Accel-Buffering': 'no'},
                                 background=BackgroundTask(record))

    @app.post('/agent/topic-check')
    def topic_check(body: TopicCheckBody) -> dict:
        """Does the composer's text belong to the chat it is about to be sent to?

        Called BEFORE the turn, which is what makes the nudge cheap and
        reversible: nothing has been spent, and there is no answer sitting in the
        wrong chat to move afterwards. It only reports — the browser offers the
        choice and the user makes it.

        Never raises. `detect_drift` fails open on its own, and a malformed body
        is FastAPI's 422; there is no failure mode here that should stand between
        the user and their own assistant.

        The `fake` embedder is withheld deliberately. `LexicalHashEmbeddings` is a
        hash of the words, so the distance between two of its vectors is an
        artefact and not a similarity — judging *semantic* drift with it is a
        category error, and it showed up as one: the offline e2e suite started
        being asked whether every substantive message was a new subject. The
        opener signal needs no model, so the nudge still works with it, and a real
        embedder is unaffected.
        """
        semantic = None if settings.embedder == 'fake' else embeddings
        verdict = detect_drift(body.recent, body.text, semantic)
        return {'changed': verdict.changed, 'score': verdict.score,
                'reason': verdict.reason}

    @app.post('/agent/transcribe')
    def transcribe(body: TranscribeBody) -> dict:
        """Speech → text. Stateless: the board is never read or written here."""
        try:
            audio = base64.b64decode(body.audio, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, f'audio is not valid base64: {exc}') from exc
        try:
            text = transcriber.transcribe(audio, body.format, model=body.model)
        except ValueError as exc:          # caller's payload was unusable
            raise HTTPException(400, str(exc)) from exc
        except TranscriptionError as exc:  # the backend let us down
            raise HTTPException(502, str(exc)) from exc
        return {'text': text}

    @app.post('/rag/reindex')
    async def reindex() -> dict:
        """Rebuild the card index. `rebuilt` is false when the board has not
        changed since the last one — the fingerprint, made observable.

        Off the client and not the snapshot: this is not a turn, and an answer
        about whether the board changed must not come from a copy of it."""
        cards = await board.list_cards()
        return {'cards': len(cards), 'rebuilt': index.build(cards)}

    @app.post('/rag/chat/reindex')
    async def chat_reindex() -> dict:
        """The boot sync on demand — an import appends to the record while the
        brain is already running, and deleting a chat removes rows from it while
        the brain is already running. `memory: false` is the honest answer rather
        than a 500: the import already succeeded into assistant.db, and the
        next boot's sync indexes it.

        The browser fires this after deleting a chat, which is what makes the
        delete reach Chroma at once instead of at the next boot."""
        if memory is None:
            return {'indexed': 0, 'memory': False}
        recorded = await board.list_all_chat()
        indexed, pruned = await memory.areconcile(recorded)
        return {'indexed': indexed, 'pruned': pruned, 'memory': True}

    @app.post('/rag/recall')
    async def recall(body: RecallBody,
                     board_id: str = Query('', alias='board')) -> dict:
        """Searches the chat record AND the board's cards; every match says
        which with `source`. `memory` says whether the chat side had anywhere
        to look.

        Without it an empty list means both "nothing was ever recorded about
        that" and "chat memory is switched off", which are opposite claims —
        one about the user's history, one about the service. The same objection
        made /rag/communities 404 rather than answer with an empty list.
        Cards never depended on Chroma, so they are searched either way; a
        board that is down costs the card half, never the whole answer.
        Chat and card hits are grouped, not sorted together: a weighted-RRF
        score and a coverage score share no scale, and interleaving them
        would pretend a calibration that was never measured.

        `?board=` scopes both halves. It is a query parameter rather than a
        body field because the browser reaches this through the Node proxy,
        which forwards the query string as it is."""
        cards: list[dict] = []
        try:
            index.build(await board.list_cards(board_id))
            # The ungated `search`: the box answers a person directly, and the
            # gate exists for contexts a model reads. It is also why this route
            # never has to wait on a model.
            hits = index.search(body.text, k=body.k)
            # Coverage over the expanded query (synonyms, other scripts) is
            # both the displayed score and the floor: a card sharing no term
            # with any spelling of the query is dense noise, not a match.
            expanded = ' '.join(expand_queries(body.text))
            scored = ((doc, round(coverage(expanded, doc.page_content,
                                           index.bm25.idf), 4))
                      for doc in hits)
            cards = [{'text': doc.page_content, 'score': score,
                      'metadata': dict(doc.metadata), 'source': 'card'}
                     for doc, score in scored if score > 0]
        except Exception as exc:
            logging.getLogger(__name__).warning(
                'recall: cards unsearchable, answering from chat only (%s)', exc)
        if memory is None:
            return {'matches': cards, 'memory': False}
        # `asearch`: this route reads the whole chat collection and ranks
        # it, and it used to do that inline from a coroutine — the one
        # remaining place a recall stalled the process (retrieval/offload.py).
        recalled = await memory.asearch(body.text, k=body.k,
                                        board_id=board_id or None)
        chat = [hit | {'source': 'chat'} for hit in recalled]
        return {'matches': chat + cards, 'memory': True}

    return app


def __getattr__(name: str):
    """Build the app when `app` is *asked for*, not when this module is imported.

    `uvicorn lodestar_brain.server:app` resolves the name with getattr, so the
    run command is unchanged — but importing the module for `create_app` no
    longer boots a brain. That mattered the moment the default embedder became a
    real model: a plain `import lodestar_brain.server` demanded the
    local-embeddings extra, so the offline test suite could not even collect.
    Constructing a service as an import side effect was always the bug; a
    dependency-free default was just hiding it."""
    if name == 'app':
        globals()['app'] = create_app()
        return globals()['app']
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
