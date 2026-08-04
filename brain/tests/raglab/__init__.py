"""RAG Lab — a test-only workbench for the diary-memory retrieval strategy.

Everything here lives under brain/tests/ on purpose: the lab reads the synthetic
fixtures (diary_year_fa.json + its ground truth), indexes them **in process
memory**, and exposes a JSON API. Nothing in production imports from here; the
lab imports production seams (its chat-model factory) so what wins an experiment
is directly portable.

**An experiment's material is not a record.** The index, the retrieved contexts
and the generated answers exist to produce one number and are discarded with the
process. What is written down is the account of the work: one JSON file per run
in .runs/, and one row per finished experiment in `ledger.py`'s SQLite. So the
lab needs **no vector database** and no service running — there is nothing to
start first, and nothing a later run can inherit from an earlier one by accident.

It serves on **:9002** — the 9000 block, alongside the brains (9000 real, 9001
the paired test brain). It must not take a board port: the lab's own page lives
*inside* the board, reached from the Assistant's "RAG test lab" button, and the
board proxies /api/raglab/* here. A lab holding :3001 would leave no test
platform to open it from.

Two front doors, one API: the board's lab page (the normal way in), and the
standalone panel this service still serves at / for running it on its own.

Start it with `npm run raglab`, which installs the embedding backend its default
embedder needs plus the optional RAGAS dependencies, or by hand:

    uv run --project brain uvicorn --app-dir brain/tests raglab.server:app --port 9002

With RAGAS (offline context metrics need rapidfuzz; the judged metrics also need
OPENROUTER_API_KEY):

    uv run --project brain --with 'ragas==0.4.*' --with 'langchain-community<0.4' \
        --with 'langchain-openai<1' --with rapidfuzz \
        uvicorn --app-dir brain/tests raglab.server:app --port 9002
"""
