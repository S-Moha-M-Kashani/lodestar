# Lodestar brain — FastAPI agent service (Python 3.13 + uv)
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Deps first, for layer caching. Both real embedder backends are installed:
# 'local-embeddings' because the production default is a Persian-tuned
# sentence-transformers model and this image must be able to load it (~1 GB of
# torch), and 'semantic' because BRAIN_EMBEDDER is overridable at run time and
# fastembed is cheap to keep reachable. tests/compose.test.js fails if the
# embedder compose pins is one this image cannot load.
RUN uv sync --frozen --no-install-project --no-dev --extra semantic --extra local-embeddings
COPY src ./src
RUN uv sync --frozen --no-dev --extra semantic --extra local-embeddings
EXPOSE 9000
CMD ["uv", "run", "--no-dev", "uvicorn", "lodestar_brain.server:app", "--host", "0.0.0.0", "--port", "9000"]
