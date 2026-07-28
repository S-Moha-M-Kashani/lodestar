# Lodestar brain — FastAPI agent service (Python 3.13 + uv)
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# deps first for layer caching; 'semantic' extra ships the real embedder
RUN uv sync --frozen --no-install-project --no-dev --extra semantic
COPY src ./src
RUN uv sync --frozen --no-dev --extra semantic
EXPOSE 9000
CMD ["uv", "run", "--no-dev", "uvicorn", "lodestar_brain.server:app", "--host", "0.0.0.0", "--port", "9000"]
