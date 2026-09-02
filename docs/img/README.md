# Release gallery manifest

The nine images and one animation beside this file are the curated release
gallery. Eight of the images are real, unretouched Playwright screenshots lifted
from `tests/artifacts/`, which the end-to-end suite writes while driving the app
against a throwaway SQLite database in a temp directory. `assistant.png` and
`approval-gate.gif` were captured separately, against a live brain on a real
model, because no artifact of the Assistant existed that told the truth about it
(see *The live capture* below). The card text in all of them comes from exactly
two sources — the six seed cards in `js/core/cards.js` and the 60-card demo
board in the tracked, already-public `sample-overview.json` — so no image carries a real person's name, a real card,
a real conversation, a key, or a path naming anyone's home directory. Nothing
was edited pixel-by-pixel; the only processing was lossless-to-the-eye
recompression (see *Optimisation* below).

Paths are stable. Reference them from the README as `docs/img/<file>` and treat
the filenames as API — rename nothing, add instead.

## The gallery

| File | Alt text | Caption (≤ 90 chars) | Supports the claim |
| --- | --- | --- | --- |
| **`hero.png`** ★ hero | Lodestar's Board view: three columns — Inbox, In Progress, Done — holding six cards, each with a ledger number, a neutral type stamp, and a coloured life-area label, above a rail of nine life-area tabs. | The board: every open thought in one ledger, stamped by type and coloured by life area. | The product at a glance — cards have a *type* and a *category*, and flow Inbox → In Progress → Done |
| `board-habits.png` | The same board grown to 62 cards across nine life areas, with a "1 habit due" banner at the top, a Habits rail on the right, and an open habit card showing a 21-day completion tape. | Habits live on the board: a due banner, a rail, and a 21-day tape per habit. | Habit cards track repetitions per period and keep a capped history; the rail is the reminder |
| `areas.png` | The Areas view: a nine-spoke radar wheel labelled Work, Love, Family, Health, Mind, Music, Travel, Home, Money, with per-area tiles below showing open counts, how long each has been carried, and a sparkline. | Nine life areas side by side — a lopsided wheel means a starved corner. | Lodestar covers a whole life, not one project; the wheel is the "give direction" pillar |
| `matrix.png` | The Matrix view: four Eisenhower quadrants — Answer now, Schedule, Delegate, Drop — each holding a category-coloured dot, with a per-area colour legend above. | The Eisenhower matrix, importance against urgency, placed by the card's own fields. | Cards carry importance and urgency, and four framings (Eisenhower, Leverage, Serenity, Follow-through) read them |
| `review.png` | The Review view scrolled to Today's Resurfacing: three long-untouched cards, each offering "Still matters", "Open" and "To Trash", above a "Stamp the review done" button showing a REVIEWED stamp. | Weekly review: three old thoughts re-met on purpose — keep, open, or let go. | Drives follow-through and never loses a thought; resurfacing is deliberate, not a notification |
| `history.png` | The Board History dialog listing timestamped changes newest first with a Restore button on each, one row marked CURRENT, and a Deleted cards section explaining that "Delete permanently" is the only thing that truly erases a card. | Every change is logged and restorable; only "Delete permanently" really erases. | The durability promise — a card is destroyed only via Trash → Delete permanently, and undo is a history, not one step |
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

All nine are PNG. The eight lifted from `tests/artifacts/` have a 1440px long
edge — already inside the ~1600px cap, so nothing was resized; `assistant.png`
was captured at 2x and halved to 1600px to sit beside them. The nine total
**915 KB**, and the GIF adds 258 KB.

Six of the eight lifted images were recompressed with a 256-colour adaptive palette (Pillow,
`MEDIANCUT`, no dither), which is visually lossless on these flat, quad-paper UI
screenshots — mean per-pixel error ≈ 0.1/255 — for a 62 % saving:

| File | Before | After |
| --- | --- | --- |
| `hero.png` | 130 KB | 47 KB |
| `board-habits.png` | 198 KB | 73 KB |
| `areas.png` | 145 KB | 54 KB |
| `matrix.png` | 104 KB | 39 KB |
| `review.png` | 129 KB | 48 KB |
| `mobile.png` | 68 KB | 25 KB |
| `history.png` | 224 KB | 218 KB (full colour) |
| `categories.png` | 210 KB | 202 KB (full colour) |

`history.png` and `categories.png` are deliberately **not** quantised. Both are
modal dialogs over a blurred backdrop, and the blur's gradient consumes the
palette: the colour swatches in `categories.png` — the one image whose subject
*is* colour — collapsed into browns, and the logo's gradient went grey. They
keep full RGB and a plain zlib re-pack instead. Any future re-optimisation must
re-check those two by eye rather than by file size.

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

## Still imperfect

- **`board-habits.png` contains a test-probe card**, C-061 "EFFORT-CONTROL-probe"
  near the top of the Inbox. Synthetic, not private, and the reason that shot is
  not the hero — but a recapture against a live board would lose it.
- **`history.png`'s log is mostly test-probe rows** — seven of nine visible
  entries are "Menu probe card". It demonstrates the durability promise
  correctly, including the "Delete permanently" row, but it reads as scaffolding.
  Recapturing it needs a scripted session on a live board rather than a lift
  from `tests/artifacts/`.
