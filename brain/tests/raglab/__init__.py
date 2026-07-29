"""RAG Lab — a test-only workbench for the diary-memory retrieval strategy.

Everything here lives under brain/tests/ on purpose: the lab reads the synthetic
fixtures (diary_year_fa.json + its ground truth), indexes them into a dedicated
Chroma database ('lodestar-raglab', or fully in-process with
BRAIN_CHROMA_URL=memory), and exposes a JSON API. Nothing in production imports
from here; the lab imports production seams (Embedder, LLMProvider,
flatten_metadata) so what wins an experiment is directly portable.

It serves on **:9002** — the 9000 block, alongside the brains (9000 real, 9001
the paired test brain). It must not take a board port: the lab's own page lives
*inside* the board, reached from the Assistant's "RAG test lab" button, and the
board proxies /api/raglab/* here. A lab holding :3001 would leave no test
platform to open it from.

Two front doors, one API: the board's lab page (the normal way in), and the
standalone panel this service still serves at / for running it on its own.

Start it with `npm run raglab`, which pins the lab's Chroma database and the
optional RAGAS dependencies, or by hand:

    uv run --project brain uvicorn --app-dir brain/tests raglab.server:app --port 9002

With RAGAS (offline context metrics need rapidfuzz; the judged metrics also need
OPENROUTER_API_KEY):

    uv run --project brain --with 'ragas==0.4.*' --with 'langchain-community<0.4' \
        --with 'langchain-openai<1' --with rapidfuzz \
        uvicorn --app-dir brain/tests raglab.server:app --port 9002
"""
