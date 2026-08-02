# Roadmap

What to add to Lodestar next. Every item should serve a product pillar:
reduce mental load, give direction, never lose a thought, drive follow-through.

## Next up

- [ ] Relevance score reranker — rerank retrieved candidates by a model-scored relevance score, as a substitutable seam (env-var selected, like every other brain backend). Default: `cohere/rerank-4-fast` via OpenRouter.
- [ ] Card actions redesign — the edit button sits in an awkward corner. Move it next to the card's other labels as a "+" button that opens a dropdown holding edit and the other card functions.
- [ ]

## Later

- [ ] Hierarchical summary layer (GraphRAG-style) — cluster chat history with Louvain/Leiden community detection, generate a hierarchy of summaries per community, store them in ChromaDB or Qdrant, and keep them updated as chat history grows. Context: Leiden was removed from `find_related` on 2026-08-01 because it never sat in the retrieval path (`docs/rag-architecture.md` §1); this is a different design — summaries as *retrievable documents* over the diary, not a dead `community` field on cards. Must earn its place in the RAG lab before porting to `retrieval.py`.
- [ ]

## Experts and agents

Start with one LLM and several expert configurations, not many autonomous agents.

An **expert** is a specialized role with its own prompt, workflow, knowledge sources,
memory fields, and safety rules. An **agent** is more independent: it can choose tools,
perform multiple steps, access systems, and hand work to other agents.

Use experts first. Convert an expert into an agent only when it needs tools such as
calendars, job search, email, or document analysis.

### Recommended experts

- **Planning and Discipline Coach** — organizes brain dumps, selects priorities, creates realistic daily plans, and reviews unfinished tasks.
- **Habit and Accountability Coach** — builds habits, tracks consistency, identifies obstacles, and adjusts targets without using shame.
- **Career Strategy Mentor** — helps choose roles, evaluate opportunities, prepare for interviews, and make long-term career decisions.
- **Executive Communication Coach** — improves diplomacy, disagreement, stakeholder management, leadership communication, and handling hierarchy.
- **Founder and Business Coach** — helps define offers, prioritize products, find customers, improve pricing, and organize business development.
- **Self-Reflection Guide** — helps examine perfectionism, emotional triggers, recurring patterns, values, and difficult decisions without diagnosing.
- **Relationship Communication Guide** — helps prepare conversations, express needs, set boundaries, and resolve conflicts constructively.

Later, add a **router agent** that identifies the user's need and sends the conversation
to the correct expert.

## Ideas (unscoped)

-

## Known gaps already noted elsewhere

- Injection eval in `brain/tests/evals/` — the measurement `untrusted.py` says must exist before buying a classifier.
- Question → card rename (the card *type* `question` must survive).
