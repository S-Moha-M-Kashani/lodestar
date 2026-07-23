# Lodestar — runs the SQLite-backed server. Node 23.4+ is required for the
# built-in node:sqlite module; there are no npm dependencies, so there is no
# install step. The board itself lives on a mounted volume (see
# docker-compose.yml), never inside the container, so data survives rebuilds.
FROM node:24-slim

WORKDIR /app
COPY . .

ENV PORT=3000
ENV BOARD_DB=/data/board.db
ENV NODE_NO_WARNINGS=1

# /data is a volume so the SQLite file persists independently of the container.
VOLUME ["/data"]
EXPOSE 3000

CMD ["node", "server.js"]
