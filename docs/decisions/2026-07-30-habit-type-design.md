# Habit — a sixth card type

Date: 2026-07-30 · Branch: `feat/habit-type`

## What this is

A habit is a card you do again and again: *2 times per day*, *1 time per week*,
*2 times per year*. Lodestar already knows how to hold a thought; it has no way
to hold a repetition. This adds `habit` as a sixth card type, with a target per
calendar period, optional reminder times, a tick that records each rep, and a
history that survives with the card.

It serves the "drive follow-through" pillar: the board can now tell you what
you are behind on today, not only what you were thinking about.

## The design language

Colour on this board means category, always — so a habit does **not** get a
colour of its own. Its type stamp is neutral ink like `? question` and
`✓ task`. The habit's own life area still supplies its ink.

The signature element is the **punch strip**: a row of small boxes on the quad
grid, one box per rep required in the current period. Two boxes means "2× per
day". Clicking the next open box stamps it in the card's category ink, slightly
askew, like a rubber stamp landing. Clicking the newest stamped box takes it
back. The strip is simultaneously the count, the progress, and the control —
there is nothing to explain and no second widget to build.

History is the same idea run sideways: a **tape** of past periods, one cell
each, carrying the number punched into it, dotted where the period was missed,
with ruled dates underneath like a ledger margin.

## Data model

Four new fields on a card. They live on every card but are only consulted when
`type === 'habit'`.

| Field | Type | Default | Rule |
| --- | --- | --- | --- |
| `habitFreq` | `'' \| 'daily' \| 'weekly' \| 'monthly' \| 'yearly'` | `''` | anything else coerces to `''` |
| `habitCount` | integer | `1` | clamped to 1..99 |
| `habitTimes` | `['HH:MM', …]` | `[]` | 24h, sorted, deduped, at most `habitCount` entries |
| `habitHistory` | `{ periodId: [epochMs, …] }` | `{}` | newest 400 periods kept |

SQLite columns, added by the existing boot-time `PRAGMA table_info` migration:
`habit_freq TEXT NOT NULL DEFAULT ''`, `habit_count INTEGER NOT NULL DEFAULT 1`,
`habit_times TEXT NOT NULL DEFAULT '[]'`, `habit_history TEXT NOT NULL DEFAULT '{}'`.

**Changing a card's type never clears these fields.** Stamping a habit as a task
by accident and stamping it back must not destroy a year of history — that is
the durability promise applied to the new field. Only the type decides whether
anything *reads* them.

`habitHistory` is keyed by period rather than being a flat list of timestamps
because the whole board is PUT on every save: per-period buckets keep a
long-lived daily habit at a few KB, and they are exactly the shape the tape and
the due check need. The 400-period cap bounds it at ~13 months of daily history,
400 weeks, or 400 years, and is applied newest-first on write.

## Periods

Local time, calendar boundaries — no rolling windows.

| Frequency | Period id | Resets |
| --- | --- | --- |
| daily | `2026-07-30` | local midnight |
| weekly | `2026-W31` | Monday, ISO week |
| monthly | `2026-07` | the 1st |
| yearly | `2026` | 1 January |

A habit is **due** when its type is `habit`, `habitFreq` is set, it is not in the
Done column, and `habitHistory[currentPeriod].length < habitCount`.

`habitTimes` are reminder slots only; the count is the target. An empty list
means "any time in the period" — a daily habit with no times is due from
midnight. This keeps *2× per year* sane while letting *2× per day* be precise.

## The reminder

- On entering the Board view, and again whenever a slot time passes while the
  page is open, a banner lists what is due and one short synthesized bip plays
  (WebAudio oscillator — no audio asset, no new dependency).
- The bip does not repeat for the same due item. A `🔊` toggle in the toolbar
  menu mutes sound permanently, persisted to localStorage.
- Browsers refuse audio before the first user gesture, so the first bip of a
  session may be silent. The banner is the reliable channel; the sound is a
  bonus. The banner is dismissible and returns next period.

## The habits panel

A rail on the **right** of the Board view, beside the three columns: today's
habits, each with its punch strip and `done / target` for the period. Habits
already complete stay listed, dimmed, so the panel reads as a day's ledger
rather than a nag list.

Below 1080px the board is already a horizontal scroll-snap carousel of columns,
so the rail joins it as the last panel rather than becoming a second layout
mode. Inventing a stacked-below variant for one panel would give the narrow
board two competing behaviours.

The rail appears only once a habit exists — a permanently empty panel would
cost every non-habit user a column of space. Its subtitle carries the state:
`2 due`, or `All done`.

The rail is a view over habit cards — there is no separate habit store.

## Answered → Done

The third column is relabelled **Done**. Only the title changes; `columnId`
stays `'answered'`, because the id is written into every stored card and every
saved board. A habit moved to Done is **retired**: it leaves the rail, stops
reminding, and keeps its history.

## The agent

`create_question` accepts `type: 'habit'` with optional `frequency` and
`times_per_period`, so the Assistant can propose a habit through the normal
confirmation gate. There is no tool for ticking a habit — logging a rep is the
user's act, and an agent that could punch your card would make the history
worthless.

## Tests

Written first, in the same change.

- **`tests/server.test.js`** — habit fields round-trip through `PUT /api/state`;
  an unknown frequency coerces to `''`; a count of 0 or 500 clamps to 1..99;
  `habitTimes` longer than the count is truncated; malformed `habitHistory`
  becomes `{}`; history survives a type change; a proposal can carry habit fields.
- **`tests/e2e_test.py`** — the Done column is labelled Done; a card can be
  stamped Habit and given 2× per day; the punch strip shows two boxes; punching
  one moves the rail to `1/2`; punching again clears it from due; the history
  tape opens from the card; the due banner appears on load.
- **`brain/tests/test_board_tools.py`** — `create_question` accepts the habit
  type and carries frequency and count through to the board payload.

## Out of scope

Notifications outside the tab, per-slot "you missed 08:00" detail, habit
analytics in the Review view, and reordering habits in the rail. Nothing here
blocks them later.
