"""FastAPI wiring. create_app() is the composition root: every swappable
module (LLM provider, search provider, embedder) is chosen here from Settings."""
import base64
import binascii
import json
import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import AgentResult, AgentStep, LodestarAgent
from .config import Settings, load_settings
from .llm import make_chat_model, served_models
from .retrieval import (CardIndex, ChatStore, coverage, expand_queries,
                        gate_llm, make_embeddings)
from .tools.board import BoardClient, make_board_tools
from .tools.retrieve import make_recall_tool, make_retrieve_tool
from .tools.websearch import DdgsSearch, make_search_tool
from .voice import make_transcriber
from .voice.base import TranscriptionError

# Two different events, kept apart because the frontend reacts differently:
# `mutated` means the board changed and the client should adopt server state,
# `proposed` means a card is waiting for the user's approval and only the
# proposals list needs refreshing. Creating a card no longer changes the board.
MUTATING_TOOLS = {'update_card'}
PROPOSING_TOOLS = {'create_card'}

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
    # explicit alternative because it can use billed remote models such as Nano.
    provider: Literal['ollama', 'openrouter'] | None = None


class TranscribeBody(BaseModel):
    audio: str            # base64; the browser encodes 16 kHz mono WAV
    format: str = 'wav'
    model: str | None = None


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


def _turn_json(result: AgentResult) -> dict:
    """The whole turn. Built here so the buffered route and the stream's `done`
    event cannot drift into reporting the same turn differently."""
    return {'reply': result.reply,
            'mutated': any(s.tool in MUTATING_TOOLS for s in result.steps),
            'proposed': any(s.tool in PROPOSING_TOOLS for s in result.steps),
            'steps': [_step_json(s) for s in result.steps],
            # null when the model reported nothing, so the Assistant can stay
            # silent rather than claim a turn cost zero.
            'usage': result.usage}


def _sse(event: str, data: dict) -> str:
    # json.dumps escapes newlines, so no payload can end a frame early.
    return f'event: {event}\ndata: {json.dumps(data)}\n\n'


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    board = BoardClient(settings.board_api_url)
    embeddings = make_embeddings(settings.embedder, settings,
                                 settings.embed_model)
    index = CardIndex(embeddings)
    # The gate grades with the same model that answers, so it needs no model of
    # its own. `gate_llm` is where BRAIN_GRADER is validated: an unknown value
    # raises at boot rather than leaving the gate quietly switched off.
    grader = gate_llm(settings.grader, make_chat_model(settings))
    tools = [*make_board_tools(board),
             make_search_tool(DdgsSearch()),
             make_retrieve_tool(index, board, llm=grader,
                                threshold=settings.grade_threshold)]
    memory = None
    if settings.chroma_url:
        try:
            memory = ChatStore(settings.chroma_url, embeddings,
                               collection=settings.chat_collection,
                               database=settings.chroma_database)
            tools.append(make_recall_tool(memory))
        except Exception as exc:
            # Chroma is optional infrastructure: the agent, board tools, web
            # search and card retrieval all work without it. Taking the whole
            # brain down because a container is stopped would be the worse
            # failure — so log loudly and serve on with recall unavailable.
            memory = None
            logging.getLogger(__name__).warning(
                'chat memory disabled: Chroma at %s is unreachable (%s)',
                settings.chroma_url, exc)
    if memory is not None:
        # Chroma is a derived index over assistant.db: rebuild what it missed
        # (turns recorded while it was down). Best-effort — compose starts the
        # board and the brain in no promised order, so a board that is not up
        # yet is logged, never fatal.
        try:
            added = memory.sync(board.list_chat())
            if added:
                logging.getLogger(__name__).info(
                    'chat index: %d recorded message(s) indexed at boot', added)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                'chat index not synced from the record: %s', exc)
    agent = LodestarAgent(settings=settings, tools=tools,
                          max_steps=settings.max_agent_steps)
    transcriber = make_transcriber(settings)

    app = FastAPI(title='lodestar-brain')

    @app.get('/health')
    def health() -> dict:
        return {'ok': True, 'service': 'lodestar-brain'}

    @app.get('/agent/models')
    def models() -> dict:
        """Which models this brain can serve, so the picker cannot offer one it
        cannot load. Under /agent/ because that is the prefix the board proxies.

        Only a local backend answers with a list: see `served_models`."""
        return served_models(settings)

    def remember(messages: list[dict], reply: str) -> None:
        """Record both sides of the exchange — durably first (assistant.db,
        through the Node API like every write), then into the derived Chroma
        index. Every chat route must call this: a second route is a second
        place to forget. Failures are logged, never raised — the reply the
        user is already reading must not become a 500 after the fact, and a
        turn the record missed is still picked up by the next boot's sync
        only if it was recorded, so the log line is the whole trace."""
        last_user = next((m.get('content', '') for m in reversed(messages)
                          if m.get('role') == 'user'), '')
        turn = [{'role': role, 'content': content}
                for role, content in (('user', last_user), ('assistant', reply))
                if content.strip()]   # the record refuses empty rows
        if not turn:
            return
        try:
            rows = board.record_chat(turn)
        except Exception:
            logging.getLogger(__name__).exception(
                'chat record unreachable — this turn is NOT in assistant.db')
            return
        if memory is None:
            return
        try:
            memory.index_messages(rows)
        except Exception:
            # Recorded but not indexed: the next boot's sync rebuilds this.
            logging.getLogger(__name__).exception('chat index write failed')

    @app.post('/agent/chat')
    async def chat(body: ChatBody) -> dict:
        # Async because cycle 2's MCP tools are coroutine-only. Safe today: the
        # sync tools (board HTTP, ddgs, Chroma) run in LangChain's thread
        # executor, so nothing here blocks the event loop.
        _refuse_if_oversized(body.messages)
        result = await agent.arun(body.messages, model=body.model,
                                  provider=body.provider)
        remember(body.messages, result.reply)
        return _turn_json(result)

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

        async def events():
            try:
                async for kind, payload in agent.astream(
                        body.messages, model=body.model, provider=body.provider):
                    if kind == 'calling':
                        yield _sse('calling', payload)   # already {tool, arguments}
                    elif kind == 'step':
                        yield _sse('step', _step_json(payload))
                    elif kind == 'token':
                        yield _sse('token', {'text': payload})
                    else:
                        remember(body.messages, payload.reply)
                        yield _sse('done', _turn_json(payload))
            except Exception as exc:
                # The headers left long ago, so there is no status code to fail
                # with. Staying quiet would leave the browser on "Thinking…"
                # forever — the hang this route exists to remove.
                logging.getLogger(__name__).exception('chat stream failed')
                yield _sse('error', {'message': str(exc)})

        # no-cache and no buffering: an intermediary holding the frames back
        # would deliver a correct transcript and none of the progress.
        return StreamingResponse(events(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache',
                                          'X-Accel-Buffering': 'no'})

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
    def reindex() -> dict:
        """Rebuild the card index. `rebuilt` is false when the board has not
        changed since the last one — the fingerprint, made observable."""
        cards = board.list_cards()
        return {'cards': len(cards), 'rebuilt': index.build(cards)}

    @app.post('/rag/chat/reindex')
    def chat_reindex() -> dict:
        """The boot sync on demand — an import appends to the record while the
        brain is already running. `memory: false` is the honest answer rather
        than a 500: the import already succeeded into assistant.db, and the
        next boot's sync indexes it."""
        if memory is None:
            return {'indexed': 0, 'memory': False}
        return {'indexed': memory.sync(board.list_chat()), 'memory': True}

    @app.post('/rag/recall')
    def recall(body: RecallBody) -> dict:
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
        would pretend a calibration that was never measured."""
        cards: list[dict] = []
        try:
            index.build(board.list_cards())
            # llm=None: the search box answers directly, so it gets the fast
            # ungated pipeline — the gate exists for contexts a model reads.
            hits = index.search(body.text, k=body.k, llm=None)
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
        chat = [hit | {'source': 'chat'}
                for hit in memory.search(body.text, k=body.k)]
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
