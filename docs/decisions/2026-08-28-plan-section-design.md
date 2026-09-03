# The plan section — when a card is meant to happen

Date: 2026-08-28 · Branch: `feature/plan-section`

## What this is

Today a card can say *what* (title, type, category) and *when at the latest*
(deadline). It cannot say **when I mean to do it**, which is a different fact:
"this March", "some time in 2028", "on the 4th". `plan` is currently a card
*type*, which is the wrong shape — a plan is not a kind of thought, it is a
date attached to one.

So:

- **`plan` stops being a type.** Existing plan cards become tasks.
- **Every card gets a plan** — a year, a year+month, or a year+month+day.
  Questions, problems, tasks and ideas all can be planned; habits cannot,
  because a habit already repeats on a calendar of its own.
- **`dream` becomes a card type** — the sixth, in the place `plan` left. It is
  a type and not a life area on purpose: a dream still belongs to travel, or
  love, or home, and a card has only one category to spend. A dream carries
  every field a task does, the plan included.
- **The rail's Plan block** shows the planned cards grouped by how near they
  are: Day, Week, Month, Year, Next year, Dreams. Ticking a card's box finishes
  it.
- **The Plan block ignores the board's filters** until you click its PLAN
  heading, which is the switch.

## Decisions already taken

| Question | Decision |
| --- | --- |
| Cards stamped `plan` | become `task`, every other field kept |
| Deadline ↔ plan | the deadline fills the plan; a hand-edited plan is yours from then on and the deadline never overwrites it again. Setting a plan never moves the deadline. |
| Rail layout | **both**, chosen in the ⚙ menu: `Plan ◂ → Stacked · Dropdown`. Stacked is the default. |
| Dream | a card *type*, not a life area — and a dated dream is listed twice: under its date, and under Dreams |
| The filter switch | no standing button: the PLAN heading is the control. Hovering it pops up what a click will do; while the filters are applied the heading is marked `filtered` |

## Data model

Two new fields on every card.

| Field | Type | Default | Rule |
| --- | --- | --- | --- |
| `plan` | `'' \| 'YYYY' \| 'YYYY-MM' \| 'YYYY-MM-DD'` | `''` | a *partial* ISO date; anything else coerces to `''` |
| `planSrc` | `'auto' \| 'user' \| 'ai'` | `'auto'` | who set it; `auto` means "mirror the deadline" |

**Why one partial-date string rather than three numbers.** The rule "a day
needs a month, a month needs a year" was the fiddly part of the request. As
three fields (`planYear`, `planMonth`, `planDay`) it is a validation rule that
every writer — the dialog, the importer, the server, the brain — has to be
trusted to apply. As one string it is *structural*: `'2027-03'` cannot carry a
day without a month, the precision is readable off the length, it sorts
correctly as text, a deadline copies into it verbatim, and it is one SQLite
column and one JSON field instead of three. The dialog still presents three
dropdowns (Year · Month · Day) and assembles them; the cascade lives in that
one place, in the UI, where the user can see it.

Validation (`planVal`, in `js/core/plan.js`):

- shape must be `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, year `1900..2999`
- month `01..12`; day must exist in that month (a `Date` round-trip, as
  `deadlineVal` already does) — a bad tail is dropped, not the whole value, so
  `'2027-02-30'` becomes `'2027-02'` rather than `''`
- anything else → `''`

**The deadline link.** After every card write (`sanitizeCard`, the dialog, the
card menu, an import, the server, an approved AI edit):

```
planSrc === 'auto'  →  plan = deadline        (so '' when there is no deadline)
planSrc === 'user'  →  plan stays exactly as it is
```

The dialog sets `planSrc = 'user'` the moment a plan dropdown is touched, and
offers `↻ follow the deadline` to put it back to `auto`. An emptied plan on a
dated card is therefore a real state ("dated, but I have not decided when to
do it") — that is what `planSrc = 'user'` with `plan = ''` means.

## The one hard error: a plan after the deadline

A plan that starts **after** the deadline is not a preference, it is a
contradiction — you cannot intend to begin a thing after it was due. So this
is the single rule in the app that refuses a save:

```
planStart(plan) > deadline   →   error, and the card cannot be saved
```

`planStart` is the earliest day the plan covers: `'2027'` → `2027-01-01`,
`'2027-03'` → `2027-03-01`, `'2027-03-04'` → itself. The *start*, not the end,
so "planned some time in 2026, due 1 September 2026" stays legal — the plan
still has room before the deadline. Only a plan whose whole window opens after
the deadline is refused.

Where it bites:

- **The card dialog** — the error appears under the Plan fieldset the moment a
  dropdown makes it true, and `Save` is refused (the submit is cancelled, the
  message is announced, focus goes to the plan row). No silent correction, and
  nothing is written.
- **The `Plan ▸` card menu** — a quick entry that would conflict refuses and
  says why, instead of committing.
- **An AI edit** — `update_card` validates the pair and returns the error to
  the assistant rather than proposing an impossible card.
- **Import and the server accept it and flag it.** A whole board is never
  rejected over one field, and silently clearing someone's plan is worse than
  showing it: such a card wears the error in red on its face and in the dialog
  until it is fixed. The invariant is enforced where a human is typing; storage
  stays forgiving, exactly as `deadlineVal` and the habit fields already are.

**Changing a card's type never clears the plan** — the same durability promise
the habit fields carry. Only the type decides whether anything reads it: a
habit's plan is ignored and its plan block is hidden.

## Where a card lands in the rail

Each planned card has exactly **one home** in stacked mode, the finest that
fits, so nothing is listed twice:

| Section | Holds |
| --- | --- |
| `DAY` | plans dated **today or earlier** (overdue is nearest, never dropped — at any precision) |
| `WEEK` | day-precision plans later in this ISO week (Mon–Sun, the week habits already count) |
| `MONTH` | plans landing in this calendar month — day-precision beyond this week, or month-precision for this month |
| `YEAR` | the rest of this calendar year: later months, and a year-precision plan for this year |
| `NEXT YEAR` | next year — and the years after it, whose rows carry their own dates |
| `DREAMS` | every dream (`type === 'dream'`), dated or not |

Dreams is the one list a card can be in **as well as** another: a dream planned
for March is listed under the month, because that is when it happens, and under
Dreams, because that is what it is. Every other card has exactly one home.

In **dropdown** mode the horizons are cumulative instead, which is what the
words mean when read one at a time: *today* = DAY; *this week* = DAY+WEEK;
*this month* = +MONTH; *this year* = +YEAR; *next year* = +NEXT YEAR; *life
dream* = DREAMS alone.

Excluded everywhere: cards in Done (`columnId === 'answered'`), habits, and
cards with no plan at all (a dream needs no plan to be listed).

## The board's filters

The Plan block is deliberately outside the board's filter chain: the point of
a plan is to see the day whole, and a category tab left on from an hour ago
would silently hide half of it.

The switch is the **PLAN heading itself** — there is no standing button, because
the plan is a list to read and a control parked above it earns its space only
when someone reaches for it. Hovering (or focusing) the heading pops up what a
click will do, `apply board filters` / `remove board filters`; while they are
applied the heading wears a small `filtered` mark, so a short list is never a
mystery. Off by default, remembered per browser, and it runs each row through
the existing `matchesFilters()`.

## What changes, file by file

**Core (`js/`)**

- `core/constants.js` — `TYPES` loses `'plan'`; `TYPE_META.plan` goes.
- `core/cards.js` — `sanitizeCard` gains `plan`/`planSrc`, migrates
  `type: 'plan' → 'task'`, and applies the deadline link. Seeds re-typed.
- `core/constants.js` — `dream` joins `TYPES` and `TYPE_META` (glyph `☾`), in
  the place `plan` left. The life-area registry is untouched: an early cut of
  this made Dream a category, and it was wrong — a dream needs its own life
  area, not to spend one.
- `core/plan.js` — rewritten: `planVal`, `planPrecision`, `planFromDeadline`,
  `planSection` (the table above), `planGroups(cards, …)` for stacked mode and
  `planCardsIn(cards, horizon, …)` for dropdown mode. Still imports nothing, so
  node can unit-test it.
- `core/sync.js` — `plan` and `planSrc` join the board fingerprint, or a save
  that only re-plans a card would be judged "already in sync" and dropped.

**UI (`js/ui/`, `index.html`, `styles.css`)**

- `edit-dialog.js` + `index.html` — a Plan fieldset (Year · Month · Day, the
  cascade, the follow-the-deadline reset, the mismatch hint), hidden for
  habits. The type filter's `→ Plans` option goes.
- `card-menu.js` — `Plan ▸` beside `Deadline ▸`: *This year*, *This month*,
  *Today*, *Clear*, mirroring how the deadline menu already works.
- `board.js` — a plan chip on the card face beside the deadline chip, in the
  card's own ink, `→ 2027-03`.
- `plan.js` — the two layouts, the five sections, the filter toggle.
- `toolbar.js` + the ⚙ menu — `Plan ◂` flyout: Stacked · Dropdown.

**Server (`server.js`)**

- two columns via the boot-time `PRAGMA table_info` migration:
  `plan TEXT NOT NULL DEFAULT ''`, `plan_src TEXT NOT NULL DEFAULT 'auto'`
- the same validators, mirrored (the server never trusts the client), the
  `plan`→`task` type migration on write, `rowToCard`, both upsert statements,
  and the proposal/edit field whitelist.

**Import / export / Asana**

- `ui/transfer.js` — the import schema text is the file-format reference shown
  in the app: drop `plan` from the type list, document `plan`/`planSrc`, and
  name `dream` among the default categories.
- `core/asana.js` — nothing new to map: `due_on` already becomes the deadline
  and the plan follows it through the deadline link.
- Export needs no change: it writes whole cards.

**Brain (`brain/`)**

- `tools/board.py` — `TYPES`/`CardType` lose `plan`; `create_card` and
  `update_card` gain one `plan` string argument documented as
  `YYYY | YYYY-MM | YYYY-MM-DD`, plus the plan in the card summary the tools
  return.
- `retrieval/chunking.py` — `card_text` gains a `plan 2027-03` phrase so
  "what am I planning in March" can hit it lexically; `card_document` metadata
  and `CARD_META_KEYS` gain `plan` (the string) and `plan_day` (an int like
  `day_int`, `20270300` for month precision, `20270000` for a year) so a scope
  filter can compare numbers.
- Re-index: the existing full re-index endpoint covers it; no migration.

## Tests

Following the repo's three layers, and kept small:

- **Unit (`tests/plan.test.js`)** — `planVal`'s cascade and bad tails, the
  deadline link in both directions, the plan-after-deadline error including the
  partial-precision cases, `planSection`'s five homes including overdue and the
  ISO-week edge, `planGroups` listing each card once.
- **Unit (`tests/server.test.js`)** — a card PUT with a plan comes back with
  it; a `type: 'plan'` card is stored as a task.
- **E2E (`tests/e2e_test.py`)** — extend the existing plan block: set a plan
  from the dialog, see the card in the right section; a deadline auto-fills the
  plan; a hand-set plan survives a deadline change; a plan after the deadline
  shows the error and the dialog refuses to save; the filter toggle narrows the
  block and only when pressed; the dream category lands in Dreams; ticking
  still finishes the card.
- **Brain (`brain/tests`)** — `card_text`/`card_document` carry the plan;
  `create_card` rejects `plan` as a type.

## Order of work

Each section is one commit on `feature/plan-section`, tests first, node suite
green before moving on. All eight landed on 2026-08-28; the closing run was
156 node checks, 310 brain tests and 469 end-to-end checks, all green.

1. `core/plan.js` + `core/cards.js` — the field, the validation, the deadline
   link, the sections. Unit tests.
2. Types and categories — `plan` off the type list, cards migrated, `dream`
   added, type filter option removed, seeds fixed.
3. The card dialog and the card face — the three dropdowns, the reset, the
   plan-after-deadline error that blocks the save, the chip, the `Plan ▸` menu.
4. The rail — five sections, stacked and dropdown, the filter toggle, the ⚙
   setting.
5. The server — columns, validators, mappings, fingerprint.
6. Import/export text and the Asana path.
7. The brain — tools and embedding metadata, with its pytest.
8. E2E block and a full-suite run.

## Risks

- **A board saved by an older browser** still holds `type: 'plan'` cards and a
  registry without `dream`. Both migrations run on load, on import and on the
  server, so whichever writes first, the other sees a legal board.
- **The deadline link is the part that can surprise.** Anyone who sets a plan
  by hand and then changes the deadline will find the plan unmoved — and if the
  new deadline lands before the plan, the next save of that card is refused
  until one of the two moves. That is deliberate: the pair has to stay possible.
- **One card, two dates** is a real cost of not merging plan and deadline. The
  alternative loses "due Sept 1, but I plan to start it in March", which is the
  ordinary case for anything worth planning.
