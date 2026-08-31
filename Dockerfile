# Lodestar — runs the SQLite-backed server. Node 23.4+ is required for the
# built-in node:sqlite module. There is still no install step here, but the
# reason changed on 2026-08-31: the repo now has one npm dependency (`pg`, for
# the migration script) and server.js does not import it. The moment the server
# itself reaches for `pg`, this image owes an `npm ci` — deliberately not added
# yet, because it lands with the read-only tree mount and a container rebuild to
# verify, not with a comment. The board itself lives on a mounted volume (see
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
