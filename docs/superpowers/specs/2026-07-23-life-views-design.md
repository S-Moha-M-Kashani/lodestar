# Life views — design

**Date:** 2026-07-23 · **Status:** approved scope: everything below (user chose "everything incl. phase 2")

Lodestar grows from a question board into a whole-life orientation board. This spec covers:
t-SNE in the Overview, the Matrix view becoming four matrices (with the Eisenhower x-axis
flipped), two new auto-defaulted card fields, an **Areas** view (small multiples + attention
wheel + per-category panels), a **Review** view (weekly review + resurfacing), a money
category, and ~60 seed questions. It is grounded in a three-strand research pass
(prioritization matrices, whole-life dashboards, category-specific presentations); key
sources cited inline.

## Why these views

The board already answers *workflow* (Board), *inventory* (Backlog), *similarity*
(Overview), and *priority* (Matrix). Research across GTD, Covey, CBT worry triage, kanban
aging, and Life-OS dashboard practice shows two orientation questions none of them answer:

- **Balance** — "which part of my life is starved?" (Wheel of Life, small multiples)
- **Staleness** — "what am I silently dropping?" (kanban card aging, GTD weekly review)

Both are computable from fields that already exist (`category`, `createdAt`, `updatedAt`,
status column). The single highest-value *new* field for a life board is **control**
(Covey circles / CBT Worry Tree: you can only dismiss — not just rank — a worry once you
record whether you can act on it). The second is **effort** (unlocks impact-effort
triage). Everything needing more numeric entry (RICE, cost-of-delay) scored poorly for
life-thoughts and is excluded.

## 1. Data model

### 1.1 New card fields

| Field | Values | Default | Meaning |
|---|---|---|---|
| `effort` | `low` \| `medium` \| `high` | `medium` | how much work answering/doing this takes |
| `control` | `act` \| `influence` \| `none` | `influence` | can I act on it, only influence it, or neither |
| `effortSrc` | `default` \| `user` \| `ai` | `default` | provenance of `effort` |
| `controlSrc` | `default` \| `user` \| `ai` | `default` | provenance of `control` |

Rules (user requirement): every card — existing and new — gets the middle value by
default; values are always editable in the card editor (stamp-style 3-way selectors,
matching the importance/urgency controls); the `*Src` markers exist so the brain can later
LLM-estimate **only** values still at `default` and stamp them `ai`, never overwriting a
`user` choice. Editing a value in the editor sets its `*Src` to `user`. LLM estimation
itself is **out of scope** for this round; the schema merely enables it.

- `app.js` `sanitizeCard` gains the four fields with defaulting.
- `server.js` adds four columns via the existing `PRAGMA table_info` boot migration
  (`effort`, `control`, `effort_src`, `control_src`, TEXT with defaults). Zero npm deps,
  as ever.
- `brain/tools/board.py` card round-trip must preserve the new fields (it already sends
  full card dicts; verify nothing strips unknown keys). All invariants hold: full-state
  PUT only, no direct SQLite access from the brain.

### 1.2 New category

`CATEGORIES` gains `{ id: 'money', label: 'Money', h: 40 }` (oklch hue slot free around
amber). Chosen category families: Work/career/projects → `work`; Worries/decisions →
cross-category (`type: problem` + `#decision` tag); Health/habits/self → `health` +
`mind`; Money/practical life → `money` + `home`.

## 2. Overview — t-SNE toggle (design approved earlier, unchanged)

- Hand-rolled exact t-SNE (~120 lines) beside `pca2()`: pairwise distances →
  perplexity-calibrated affinities (perplexity `min(15, ⌊(n−1)/3⌋)`) → ~350 gradient
  iterations with early exaggeration, **initialized from the PCA result**, seeded PRNG
  (mulberry32) → deterministic. O(n²) is fine to a few hundred cards; runs in a chunked
  async loop yielding to the browser; dots CSS-slide to new positions like the existing
  semantic upgrade. Results cached per vector-set hash.
- No CDN library: keeps the "map never needs the network" property and determinism.
- UI: `.plot-proj-toggle` on the plot head, two buttons `data-proj="pca"|"tsne"` with
  `aria-pressed`. t-SNE relabels axes `t-SNE-1/2` and appends "· t-SNE layout" to the
  status line. Preference stored in `localStorage` (never in board state, so it is never
  PUT to the server).
- 0–1 cards: current behavior. 2–3 cards: silently uses PCA (perplexity needs n ≥ 4).
  Degenerate spread: existing ring fallback in `normalizePoints`.

## 3. Matrix → Matrices (picker + flipped Eisenhower)

A stamp-style picker (`.matrix-switch`, buttons `data-matrix`, `aria-pressed`) on the
matrix plate. All lenses reuse the quadrant-grid + dot components; the grid becomes
column-count-flexible (2×2 or 2×3). Importance stays the y-axis everywhere. Choice
persists in `localStorage`.

| id | Axes (y × x) | Cells (top row / bottom row, left→right) | Placement rule |
|---|---|---|---|
| `eisenhower` (default) | importance ↑ × **urgency ←** (flipped) | Answer now, Schedule / Delegate, Drop | needs importance & urgency set (unchanged) |
| `leverage` | importance ↑ × effort → (low\|med\|high) | Quick win, Solid bet, Big bet / Fill-in, Meh, Time sink | needs importance; effort always present |
| `serenity` | importance ↑ × control → (act\|influence\|none) | Act now, Nudge, Accept & plan / Easy win, Mention it, Let go | needs importance; control always present |
| `followthrough` | importance ↑ × age → (fresh\|aging\|stale) | On it, Watch, **Rescue** / Fine, Fine, Let go? | age from `updatedAt`: fresh < 14d, aging 14–45d, stale > 45d; open cards only (not `answered`) |

The x-flip: reorder `QUADRANTS` so high-urgency is the left column and label the axis
`← URGENCY` — "Answer now" lands top-left, the classic Eisenhower orientation.
Sources: Covey circles of control; CBT Worry Tree (getselfhelp.co.uk); kanban card
aging (Kanban Zone); impact-effort matrix (ProductPlan).

## 4. Areas view (new)

`VIEWS` gains `areas`. One uniform **small-multiples tile per category in use**
(`.areas-grid` > `.area-tile`, category-colored tab edge), each showing: category label,
open count, oldest-open age ("carrying 8 months"), the top-importance open question title,
a 12-week activity sparkline (cards created/updated per week, inline SVG), and an
ink-fade staleness tint the older the oldest item. Clicking a tile sets the app's
category filter and opens the **area detail** beneath the grid (`.area-detail`).

**Attention wheel** (`.wheel`, inline SVG radar) heads the view: one spoke per category
in use, spoke value = open-question mass (count, high-importance ×2), normalized. A
lopsided life reads as a lopsided wheel. Derived only — no satisfaction scoring ritual.

**Area detail = category-specific panel** (research strand c: different categories
deserve different presentations; one navigation pattern hosts them all):

- tag `purchase` present in the area → **cooling-off list**: each purchase-tagged open
  card with days remaining in a 30-day window from `createdAt`; matured items flagged
  "decide now"; a running count of purchase cards sent to Trash ("resisted"). (30-day
  rule; SoFi/Finny.)
- category `mind` (learning) → **progress bars**: per co-tag stacked open-vs-answered
  bars + a cumulative asked-vs-answered burn-up line. (Learning-roadmap convention.)
- cards with `type: problem` in the area → **serenity strip**: the area's problems
  grouped act / influence / none, with a standing hint to review the `none` group during
  a weekly "worry window" (CBT worry postponement — clinically supported).
- default (all areas) → **staleness list**: open cards sorted oldest-`updatedAt` first
  with age labels (personal-CRM "last touched" convention).

## 5. Review view (new)

`VIEWS` gains `review` (`.review-sheet`). GTD's weekly review as a screen — the ritual is
what makes every other view trustworthy:

- **Stat tiles**: inbox count to triage, answered this week, new this week, open total.
- **Deltas**: this week vs last (created/answered), per category.
- **Neglect list**: high-importance open cards untouched > 30 days.
- **Resurfacing panel** (`.resurface-card`): 3 cards sampled daily, deterministic on the
  date (seeded PRNG on YYYY-MM-DD), biased to stale × important (Readwise-style
  re-encounter). Actions per card: **Still matters** (bumps `updatedAt`), **Open**
  (editor), **To Trash** (existing soft-delete path only — durability promise intact).
- **"Reviewed" stamp**: a button stamping the review done; `lastReviewedAt` in
  `localStorage` (device-local by design; not board state). Header shows "last reviewed
  N days ago" everywhere the ledger aesthetic allows (small note on the Review tab).

**Decisional balance (minor editor enhancement):** for cards tagged `decision`, notes
lines starting with `+` / `−` (or `-`) render as a two-column pro/con sheet in the card
editor preview (decisional balance sheet, MI/CBT convention). Pure rendering; no schema.

## 6. Seed data

`sample-overview.json` rewritten to the **current schema** (it still uses the retired
`priority` field — import silently drops it today) and grown to **~60 cards**: all 9
categories, all 5 types, `createdAt` spread over ~6 months, a mix of set/unset
importance/urgency, varied effort/control (some `user`-provenance, most `default`),
tags including `purchase`, `decision`, learning co-tags, and a handful answered — so the
t-SNE map clusters, every matrix cell is inhabited, the wheel is lopsided, cooling-off
and progress panels have data, and resurfacing has stale material. The in-app
`seedCards()` boot seeds stay as they are.

## 7. Testing (e2e, offline as today)

New checks in `tests/e2e_test.py` (CSS classes above are test-stable API; nothing
renamed):

- Overview: toggle renders, PCA pressed by default; t-SNE keeps one dot per question,
  changes axis labels/status, and moves dots (coords differ from PCA); toggling back
  restores; preference survives leaving/re-entering the view.
- Matrices: picker renders 4 options; Eisenhower "Answer now" is the **top-left** cell;
  each lens shows dots; followthrough buckets by age; leverage/serenity place every
  card with importance set (defaults fill the middle column).
- Editor: effort/control selectors exist, default to middle, editing sets them and
  survives reload (server round-trip).
- Areas: one tile per category in use; wheel SVG present; clicking a tile opens the
  area detail; purchase/learning/problem panels appear for seeded data.
- Review: stat tiles present; resurfacing shows 3 cards; "Still matters" bumps the
  card's updated stamp; Reviewed stamp persists in localStorage.
- Import: the grown sample file imports; card count matches; new fields survive a
  save/reload cycle.

Brain unit tests: extend `test_board` round-trip to include the new fields.

## 8. Edge cases

- Boards with < 4 cards: t-SNE falls back to PCA; matrices/areas render normally.
- No cards in a category: no tile; wheel omits the spoke.
- All `updatedAt` equal (fresh import): followthrough puts everything in one column —
  acceptable; seed data spreads dates to avoid it in the demo.
- `localStorage` unavailable: preferences degrade to in-session defaults.
- Theme change transitions: e2e waits for settling before sampling colors (existing gotcha).

## 9. Out of scope (explicitly)

- LLM estimation of effort/control (enabled by `*Src`, built later in the brain).
- Numeric prices on purchases, satisfaction scoring on the wheel, card-to-card links /
  OKR trees, Ikigai and Cynefin classifications (research-rejected: low fit or high
  entry cost).

## 10. Research sources (selection)

Covey circles of control (modern.works, leadingsapiens.com) · CBT Worry Tree
(getselfhelp.co.uk) and worry postponement (Springer meta-analysis, PMC RCT) ·
impact-effort matrix (ProductPlan, BiteSize) · kanban card aging / aging WIP (Kanban
Zone, Businessmap) · GTD weekly review & Horizons (gettingthingsdone.com, Super
Productivity) · Wheel of Life (Mindtools, Scott Jeffrey) · small multiples (Tufte via
Wikipedia; Pew Research) · Readwise resurfacing (blog.readwise.io) · Bullet-journal
migration/collections (bulletjournal.com) · 30-day purchase rule (SoFi) · personal-CRM
staleness (Wave Connect, Nat Eliason) · faceted browsing for personal data (Microsoft
Research FacetMap/FacetLens) · Obsidian graph-view hairball caution (codeculture.store).
