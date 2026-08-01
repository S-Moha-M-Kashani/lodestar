"""FastAPI wiring. create_app() is the composition root: every swappable
module (LLM provider, search provider, embedder) is chosen here from Settings."""
import base64
import binascii
import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .agent.registry import build_agent
from .config import Settings, load_settings
from .llm.factory import make_chat_model, served_models
from .retrieval import CardIndex, ChatStore, gate_llm, make_embeddings
from .tools.board import BoardClient, make_board_tools
from .tools.retrieve import make_recall_tool, make_retrieve_tool
from .tools.websearch import DdgsSearch, make_search_tool
from .voice import make_transcriber
from .voice.base import TranscriptionError

# Two different events, kept apart because the frontend reacts differently:
# `mutated` means the board changed and the client should adopt server state,
# `proposed` means a card is waiting for the user's approval and only the
# proposals list needs refreshing. Creating a card no longer changes the board.
MUTATING_TOOLS = {'update_question'}
PROPOSING_TOOLS = {'create_question'}


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
    k: int = 5


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
    agent = build_agent("default", settings=settings, tools=tools,
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

    @app.post('/agent/chat')
    async def chat(body: ChatBody) -> dict:
        # Async because cycle 2's MCP tools are coroutine-only. Safe today: the
        # sync tools (board HTTP, ddgs, Chroma) run in LangChain's thread
        # executor, so nothing here blocks the event loop.
        result = await agent.arun(body.messages, model=body.model,
                                  provider=body.provider)
        if memory is not None:
            last_user = next((m.get('content', '') for m in reversed(body.messages)
                              if m.get('role') == 'user'), '')
            memory.record([last_user], metadata={'role': 'user'})
            memory.record([result.reply], metadata={'role': 'assistant'})
        return {'reply': result.reply,
                'mutated': any(s.tool in MUTATING_TOOLS for s in result.steps),
                'proposed': any(s.tool in PROPOSING_TOOLS for s in result.steps),
                'steps': [{'tool': s.tool, 'arguments': s.arguments}
                          for s in result.steps]}

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

    @app.post('/rag/recall')
    def recall(body: RecallBody) -> dict:
        if memory is None:
            return {'matches': []}
        return {'matches': memory.search(body.text, k=body.k)}

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
