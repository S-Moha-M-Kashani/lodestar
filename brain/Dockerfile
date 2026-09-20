# Lodestar brain — FastAPI agent service (Python 3.13 + uv)
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
# Deps first, for layer caching. Both real embedder backends are installed:
# 'local-embeddings' because the production default is a Persian-tuned
# sentence-transformers model and this image must be able to load it, and
# 'semantic' because BRAIN_EMBEDDER is overridable at run time and fastembed is
# cheap to keep reachable. tests/compose.test.js fails if the embedder compose
# pins is one this image cannot load.
#
# Measured on the first real build, 2026-08-02: this image is **18.1 GB**, not
# the ~1 GB this comment used to claim. The cost is not torch itself but the
# CUDA stack it drags in by default (nvidia-cu13 wheels), none of which can run
# on a Mac and none of which is used by a CPU-only deployment either. Pinning
# the CPU-only torch wheel is the obvious fix and is deliberately not done here,
# because it is a dependency change and belongs behind the project's test-first
# gate rather than in a docs pass. Until then, budget the disk.
RUN uv sync --frozen --no-install-project --no-dev --extra semantic --extra local-embeddings
COPY src ./src
RUN uv sync --frozen --no-dev --extra semantic --extra local-embeddings

# The two subscription CLIs. Without them the composed brain reports both
# backends unavailable — correctly, since `cli_backends()` asks `shutil.which` —
# and the picker offers neither, which is what `docs/cli-backends.md` said for
# months. They are the LAST layers on purpose: each is one file and the two uv
# syncs above are the expensive ones, so bumping a version here rebuilds a
# hundred megabytes rather than eighteen gigabytes.
#
# Downloaded and checksummed rather than piped from an installer, and pinned to
# an exact version rather than `latest` — the rule the chroma image tag already
# follows one service along. A coding agent with a shell is precisely the
# program not to install from an unverified byte stream, and `latest` would mean
# two builds of the same commit shipping different binaries with nothing saying
# so. Both are single static binaries, so the image needs no Node runtime for
# either.
#
# To move a version: change the ARG, fetch the file by hand, and put the sha256
# it really has here. Never the other way round — a checksum copied from a
# release page you did not download is a checksum that has verified nothing.
ARG CODEX_VERSION=0.154.0
ARG CLAUDE_VERSION=2.1.274
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    case "$(dpkg --print-architecture)" in \
      arm64) codex_target=aarch64-unknown-linux-musl; \
             codex_sha=583b48df32804213bdcd338c2e5adb06b34340821fa757a726cc0a524fa33c27; \
             claude_platform=linux-arm64; \
             claude_sha=2db904daea17addff9de557ba26a725916888aa7b546e2c5dd989c20d9d49ab3 ;; \
      amd64) codex_target=x86_64-unknown-linux-musl; \
             codex_sha=d7e18b2597ae8f242f5f31ee9e90deef48dbc9edd634d9868fb6435d08c07f02; \
             claude_platform=linux-x64; \
             claude_sha=15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07 ;; \
      *) echo "no codex/claude build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/codex.tar.gz \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_target}.tar.gz"; \
    echo "${codex_sha}  /tmp/codex.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/codex.tar.gz -C /tmp; \
    mv "/tmp/codex-${codex_target}" /usr/local/bin/codex; \
    chmod +x /usr/local/bin/codex; \
    rm /tmp/codex.tar.gz; \
    curl -fsSL -o /usr/local/bin/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/${claude_platform}/claude"; \
    echo "${claude_sha}  /usr/local/bin/claude" | sha256sum -c -; \
    chmod +x /usr/local/bin/claude; \
    codex --version; claude --version
EXPOSE 9000
CMD ["uv", "run", "--no-dev", "uvicorn", "lodestar_brain.server:app", "--host", "0.0.0.0", "--port", "9000"]
