# Contributing to Lodestar

Thanks for helping. Bug reports, fixes, documentation and feature ideas are all welcome.

## Before you start

- **Bugs and ideas:** open an issue first, so we can agree on the approach before you write code.
- **Security problems:** do not open a public issue — see [`SECURITY.md`](SECURITY.md).
- **Never paste real board or chat content** into an issue. Reproduce on a throwaway board or the
  demo data in [`sample-overview.json`](sample-overview.json).

## Setup

You need [Node.js 23.4+](https://nodejs.org). The assistant also needs
[uv](https://docs.astral.sh/uv/) (Python 3.13); the end-to-end tests need Playwright through uv.

```sh
git clone https://github.com/S-Moha-M-Kashani/lodestar.git
cd lodestar
npm run auth:setup   # paste the printed line into .env
npm start            # http://localhost:3000
```

There is no `npm install` and no build step. [`ARCHITECTURE.md`](ARCHITECTURE.md) explains how
the pieces fit together; [`docs/decisions/`](docs/decisions/) records why.

## Tests

Every change ships with tests. All suites run offline, with no API keys.

```sh
node --test tests/*.test.js                         # server and frontend logic
uv run --project brain pytest brain/tests           # assistant ("brain")
uv run --with playwright python tests/e2e_test.py   # full browser run
```

Where tests go: server → `tests/server.test.js`, UI → `tests/e2e_test.py`,
assistant → `brain/tests/`.

## Guidelines

- **Keep it small.** Reuse what exists; avoid new dependencies, layers and abstractions unless a
  current need requires them. The server has one npm dependency and the frontend none.
- **Never send a partial card list to `PUT /api/state`** — the server treats omitted cards as
  deleted. Read the whole board, change it, send it all back.
- **CSS class names and element ids are relied on by tests.** Add new ones; don't rename.
- **Every assistant backend is chosen by an environment variable**, and an unknown value must
  fail at startup, never fall back silently.
- Use the existing colour tokens; colour always means life area.

## Pull requests

1. Fork the repository and create a branch from `master`.
2. Keep the change focused, with tests.
3. Write commit messages as [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:` …).
4. Make sure the test suites pass, then open a pull request against `master`.

By contributing you agree that your work is released under the [MIT License](LICENSE).
