# Release gallery manifest

The nine images and one animation beside this file are the curated release
gallery. Six of the images are real, unretouched Playwright screenshots lifted
from `tests/artifacts/`, which the end-to-end suite writes while driving the app
against a throwaway SQLite database in a temp directory. The other four were
captured on purpose, by two scripted sessions against a throwaway board of their
own: `assistant.png` and `approval-gate.gif`, because no artifact of the
Assistant existed that told the truth about it (*The live capture* below), and
`board-habits.png` and `history.png`, because the artifacts of those two carried
test-probe cards in frame (*The recapture* below). The card text in all of them
comes from exactly three sources — the six seed cards in `js/core/cards.js`, the
60-card demo board in the tracked, already-public `sample-overview.json`, and, in
the two recaptured shots, three plainly synthetic titles typed into the running
app (*Meditate*, *Book the winter tyres swap*, and *Sort out grandpa's photo
archive* renamed to *Scan* it). So no image carries a real person's name, a real
card, a real conversation, a key, or a path naming anyone's home directory. Nothing was
edited pixel-by-pixel; the only processing was lossless-to-the-eye recompression
(see *Optimisation* below).

Paths are stable. Reference them from the README as `docs/img/<file>` and treat
the filenames as API — rename nothing, add instead.

## The gallery

| File | Alt text | Caption (≤ 90 chars) | Supports the claim |
| --- | --- | --- | --- |
| **`hero.png`** ★ hero | Lodestar's Board view: three columns — Inbox, In Progress, Done — holding six cards, each with a ledger number, a neutral type stamp, and a coloured life-area label, above a rail of nine life-area tabs. | The board: every open thought in one ledger, stamped by type and coloured by life area. | The product at a glance — cards have a *type* and a *category*, and flow Inbox → In Progress → Done |
| `board-habits.png` | The same board grown to 61 cards across nine life areas, with a "1 habit due — Meditate (1/2 today)" banner at the top, a Habits rail on the right, and a habit card showing its punch strip and a 21-day completion tape reading "15 of 21 complete · longest run 4". | Habits live on the board: a due banner, a rail, and a 21-day tape per habit. | Habit cards track repetitions per period and keep a capped history; the rail is the reminder |
| `areas.png` | The Areas view: a nine-spoke radar wheel labelled Work, Love, Family, Health, Mind, Music, Travel, Home, Money, with per-area tiles below showing open counts, how long each has been carried, and a sparkline. | Nine life areas side by side — a lopsided wheel means a starved corner. | Lodestar covers a whole life, not one project; the wheel is the "give direction" pillar |
| `matrix.png` | The Matrix view: four Eisenhower quadrants — Answer now, Schedule, Delegate, Drop — each holding a category-coloured dot, with a per-area colour legend above. | The Eisenhower matrix, importance against urgency, placed by the card's own fields. | Cards carry importance and urgency, and four framings (Eisenhower, Leverage, Serenity, Follow-through) read them |
| `review.png` | The Review view scrolled to Today's Resurfacing: three long-untouched cards, each offering "Still matters", "Open" and "To Trash", above a "Stamp the review done" button showing a REVIEWED stamp. | Weekly review: three old thoughts re-met on purpose — keep, open, or let go. | Drives follow-through and never loses a thought; resurfacing is deliberate, not a notification |
| `history.png` | The Board History dialog listing seven timestamped changes newest first — a delete, a category set, a card added, a move to Done, a deadline, a title edit, a type change — each with a Restore button, the newest marked CURRENT, above a Deleted cards section holding the deleted card with Restore and "Delete permanently" beside it. | Every change is logged and restorable; only "Delete permanently" really erases. | The durability promise — a card is destroyed only via Trash → Delete permanently, and undo is a history, not one step |
| `categories.png` | The Categories dialog listing ten life areas, each in its own colour with its card count and a Remove button, plus a new-category field and a row of colour swatches. | Life areas are yours to name and colour — removing one never deletes its cards. | Colour always means category; the registry is per board and editable |
| `assistant.png` | The Assistant view: a question asking for a new task card, the assistant's reply saying it has proposed one, and above it a dashed panel headed "PROPOSED — 1 CARD AWAITING YOUR APPROVAL" holding the card "Book a dentist appointment" with Reject and Approve buttons. | The assistant proposes; nothing reaches the board until you approve it. | The write-protection gate — `MUTATING_TOOLS` is empty and the agent has no board-write path at all |
| `approval-gate.gif` | A 15-second animation: a question is typed into the Assistant, the reply streams in, a proposed card appears awaiting approval, Approve is pressed, and the card appears on the Board as C-061 "Book a dentist appointment" in Health. | Proposal to approval to card, in fifteen seconds — the gate in motion. | The same gate, shown end to end: the agent cannot write, and the user's click is what does |
| `mobile.png` | Lodestar at 375px wide: the header and view tabs stacked, the life-area tabs in a vertical rail, and the Inbox column beside a horizontally scrolling In Progress column. | At 375px the board becomes a horizontal scroll; nothing is hidden away. | The board is usable on a phone with no separate mobile build |

★ = hero. Use `hero.png` as the single image at the top of the README.

`approval-gate.gif` needs no sound and shows no cursor, so its caption carries
the story on its own for anyone who cannot or will not play it. The transcript
of the recorded turn, in order: the question *"Propose a new task card: Book a
dentist appointment. Health area. Do not ask me anything first."*; the reply
*"I've proposed the task card 'Book a dentist appointment' under Health, in the
inbox. It's awaiting your approval on the board."*; the panel *"PROPOSED — 1
CARD AWAITING YOUR APPROVAL"* with **Reject** and **Approve**; and after
Approve, the card on the Board as **C-061 · task · Health**.

Note on `review.png`: the shot is scrolled past the four stat tiles at the top
of the Review view, so the tiles are not visible in it. The caption above claims
only what the image shows.

## Optimisation

All nine are PNG. Eight have a 1440px long edge — the six lifted from
`tests/artifacts/` and the two recaptured, which are shot at `scale="css"` into a
1440x900 viewport and so need no resizing at all; `assistant.png` was captured at
2x and halved to 1600px to sit beside them. The nine total **1,108 KB**, and the
GIF adds 258 KB.

Five of them are recompressed with a 256-colour adaptive palette (Pillow,
`MEDIANCUT`, no dither), which is visually lossless on flat, quad-paper UI
screenshots — mean per-pixel error ≈ 0.1/255 — for a 62 % saving:

| File | Shipped | Treatment |
| --- | --- | --- |
| `hero.png` | 47 KB | 256-colour palette (from 130 KB) |
| `areas.png` | 54 KB | 256-colour palette (from 145 KB) |
| `matrix.png` | 39 KB | 256-colour palette (from 104 KB) |
| `review.png` | 48 KB | 256-colour palette (from 129 KB) |
| `mobile.png` | 25 KB | 256-colour palette (from 68 KB) |
| `board-habits.png` | 210 KB | full colour, plain re-pack |
| `history.png` | 275 KB | full colour, plain re-pack |
| `categories.png` | 202 KB | full colour, plain re-pack |
| `assistant.png` | 204 KB | halved from 2x, full colour |

Four ship in full colour. `assistant.png` simply was never quantised — it was
already the halved 2x capture. The other three are deliberate refusals, and the
reason is one artefact in three places. `history.png` and `categories.png` are
modal dialogs over a blurred
backdrop, and the blur's gradient consumes the palette: the colour swatches in
`categories.png` — the one image whose subject *is* colour — collapsed into
browns, and the logo's gradient went grey. `board-habits.png` **was** quantised
until 2026-09-02, at 73 KB, and the recapture is what showed what that had cost:
the logo's purple glow bands into visible contour rings and the banner's bell
emoji loses its yellow. Both were true of the shipped image too; nobody had
looked at that 60px patch.

So the mean error is a poor witness here. 0.1/255 averaged over 1.3 M
mostly-flat pixels says nothing about the one small region where the whole error
lives, which is why the check is now a magnified side-by-side crop of the logo,
the bell and the coloured area tabs, plain against quantised. Full colour costs
`board-habits.png` 137 KB and buys back the logo, and it is the reason
`history.png` ships at 275 KB rather than the 218 KB its predecessor managed —
that shot had two cards behind the modal and this one has 61, and a fuller board
is the more honest backdrop. Any future re-optimisation must re-check these four
**by eye** rather than by file size.

No image tool beyond `sips` and Pillow was used, and no dependency was added.

## The live capture

`assistant.png` and `approval-gate.gif` come from one recorded session on
2026-09-02, not from the test suite. They had to: the only artifact showing the
Assistant was `tests/artifacts/voice-transcript.png`, and it is unusable for a
release — the transcript is `FAKE:` replies from the offline chat backend, there
is a red "The assistant is unavailable right now" banner, and a stray "banner
please" probe sits in the margin. Rejected on accuracy, not privacy.

What was recorded, so it can be recorded again:

- A throwaway board and a real brain on `BRAIN_LLM=claude-cli` — a live model,
  so the reply, the tool count and the token figure in the images are a real
  turn's and not a fixture's. Everything else fake or in-memory:
  `BRAIN_EMBEDDER=fake`, `BRAIN_CHROMA_URL=memory`, `BRAIN_URL_SAFETY=fake`.
- The board filled from the same tracked `sample-overview.json` the e2e uses, so
  the privacy position is unchanged.
- Frames every 500 ms through the turn, then assembled by Pillow at 900px wide
  on one shared 128-colour palette — one palette for the whole animation, or
  every frame becomes a keyframe and the file triples. 26 distinct frames,
  14.8 s, 258 KB. No ffmpeg, no new dependency.
- The prompt is deliberately blunt (*"Do not ask me anything first"*). Asked
  loosely, the model quite reasonably came back with "there is already a dentist
  card — new one, or update that?", which is the right answer to the question
  and the wrong frame for a recording about the approval gate. That is what a
  live model costs and it is worth it.

Two things the recording found, both since fixed and both visible in these
images only in their corrected form: a tool's own model call being streamed into
the transcript, and the tool count being printed twice in one row.

## The recapture

`board-habits.png` and `history.png` come from one scripted session on
2026-09-02, not from the test suite. The artifacts they replace were accurate
but read as scaffolding: the habits shot carried a card called
`EFFORT-CONTROL-probe` near the top of the Inbox, and seven of the nine visible
rows in the History log said *"Menu probe card"*. Rejected on how they read, not
on privacy — but a release gallery whose evidence for the durability promise is a
log of a test poking at a menu is asking to be disbelieved.

What was recorded, so it can be recorded again:

- A throwaway board on a temp SQLite database and a brain on `BRAIN_LLM=fake`.
  Neither shot needs a model; the brain is there so nothing on screen is an error
  banner. `LODESTAR_BACKUP_ON_WRITE=0`, and `databases/real/` untouched.
- The board filled from the same tracked `sample-overview.json`, substituted, so
  the privacy position is unchanged. The six seed cards that substitution
  soft-deletes are then purged through `DELETE /api/cards/:id` — otherwise they
  would be the Deleted cards section of the History shot, which is the one part
  of that image the caption is about.
- One habit created through the card dialog, exactly as a person makes one:
  quick-add *"Meditate"*, stamp it Habit, daily, 2 per day, Health.
- Both frames shot at `scale="css"` into a 1440x900 viewport — a 1x render at the
  gallery's own size, no resampling anywhere.
- The History log is nine real actions taken through the UI in order: a move to
  In Progress, a category set to Mind, a type set to task, a title edited in the
  card dialog, a deadline set to today, a move to Done, a card added, its
  category set to Home, and a delete. The board's own log then reads like
  somebody's afternoon, because it is one.

One thing in these two images was **not** typed into the running app, and it is
the only such thing in the gallery: the twenty days of completions behind
`board-habits.png`'s 21-day tape were written into `habitHistory` through
`PUT /api/state`, then adopted back by reloading the browser. A habit created a
minute before the shutter has an empty tape, and an empty tape is a picture of
the widget rather than of the feature. The dates are synthetic, about nobody, and
carry no text.

## Still imperfect

- **`history.png` is the gallery's largest file, 275 KB.** It cannot be
  quantised (see *Optimisation*), and its backdrop is a full 61-card board
  rather than the two-card one its predecessor happened to have. Both are
  deliberate; the size is the price.
- **The 21-day tape's older completions were written, not lived** — see the
  paragraph above. Everything else in both images was done through the UI.
