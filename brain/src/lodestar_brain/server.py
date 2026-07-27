"""FastAPI wiring. create_app() is the composition root: every swappable
module (LLM provider, search provider, embedder) is chosen here from Settings."""
from fastapi import FastAPI
from pydantic import BaseModel

from .agent.registry import build_agent
from .config import Settings, load_settings
from .llm.fake import FakeProvider
from .llm.openrouter import OpenRouterProvider
from .rag.chat_memory import ChromaChatMemory, chunk_text, make_recall_tool
from .rag.embedder import make_embedder
from .rag.index import LeidenIndex, make_retrieve_tool
from .tools.board import BoardClient, make_board_tools
from .tools.websearch import DdgsSearch, make_search_tool

MUTATING_TOOLS = {'create_question', 'update_question'}


class ChatBody(BaseModel):
    messages: list[dict]
    model: str | None = None


class RecallBody(BaseModel):
    text: str
    k: int = 5


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    if settings.llm_provider == 'fake':
        llm = FakeProvider()
    else:
        llm = OpenRouterProvider(api_key=settings.openrouter_api_key,
                                 base_url=settings.openrouter_base_url,
                                 default_model=settings.model)
    board = BoardClient(settings.board_api_url)
    embedder = make_embedder(settings.embedder)
    index = LeidenIndex(embedder)
    tools = [*make_board_tools(board),
             make_search_tool(DdgsSearch()),
             make_retrieve_tool(index, board)]
    memory = None
    if settings.chat_memory_dir:
        memory = ChromaChatMemory(settings.chat_memory_dir, embedder)
        tools.append(make_recall_tool(memory))
    agent = build_agent("default", llm=llm, tools=tools, max_steps=settings.max_agent_steps)

    app = FastAPI(title='lodestar-brain')

    @app.get('/health')
    def health() -> dict:
        return {'ok': True, 'service': 'lodestar-brain'}

    @app.post('/agent/chat')
    def chat(body: ChatBody) -> dict:
        result = agent.run(body.messages, model=body.model)
        if memory is not None:
            last_user = next((m.get('content', '') for m in reversed(body.messages)
                              if m.get('role') == 'user'), '')
            memory.record(chunk_text(last_user), metadata={'role': 'user'})
            memory.record(chunk_text(result.reply), metadata={'role': 'assistant'})
        return {'reply': result.reply,
                'mutated': any(s.tool in MUTATING_TOOLS for s in result.steps),
                'steps': [{'tool': s.tool, 'arguments': s.arguments}
                          for s in result.steps]}

    @app.post('/rag/reindex')
    def reindex() -> dict:
        cards = board.list_cards()
        index.build(cards)
        return {'cards': len(cards), 'communities': len(set(index.membership))}

    @app.get('/rag/communities')
    def communities() -> dict:
        return {'communities': index.communities()}

    @app.post('/rag/recall')
    def recall(body: RecallBody) -> dict:
        if memory is None:
            return {'matches': []}
        return {'matches': memory.search(body.text, k=body.k)}

    return app


app = create_app()
