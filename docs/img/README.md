# Release gallery manifest

The eight images beside this file are the curated release gallery: one hero shot
and a seven-image tour. Every one is a real, unretouched Playwright screenshot
lifted from `tests/artifacts/`, which the end-to-end suite writes while driving
the app against a throwaway SQLite database in a temp directory. The card text
in them therefore comes from exactly two sources — the six seed cards in
`js/core/cards.js` and the 60-card demo board in the tracked, already-public
`sample-overview.json` — so no image carries a real person's name, a real card,
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
| `mobile.png` | Lodestar at 375px wide: the header and view tabs stacked, the life-area tabs in a vertical rail, and the Inbox column beside a horizontally scrolling In Progress column. | At 375px the board becomes a horizontal scroll; nothing is hidden away. | The board is usable on a phone with no separate mobile build |

★ = hero. Use `hero.png` as the single image at the top of the README.

Note on `review.png`: the shot is scrolled past the four stat tiles at the top
of the Review view, so the tiles are not visible in it. The caption above claims
only what the image shows.

## Optimisation

All eight are PNG, long edge 1440px — already inside the ~1600px cap, so nothing
was resized. The eight images total **709 KB**, down from 1,208 KB.

Six of the eight were recompressed with a 256-colour adaptive palette (Pillow,
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

## Still missing

- **No Assistant / chat screenshot.** The only artifact showing the Assistant
  view is `tests/artifacts/voice-transcript.png`, and it is unusable for a
  release: the transcript is full of `FAKE:` replies from the offline `fake`
  chat backend, it carries a red "The assistant is unavailable right now" error
  banner, and a stray "banner please" probe string sits in the margin. It was
  rejected on accuracy, not privacy. A clean Assistant shot has to be captured
  against a running brain.
- **No approval-gate GIF.** `spec.md` also requires a short animation of the
  assistant proposing a card and the user accepting it. That is the other half
  of task 2.2 and needs a live recording; it is not in `tests/artifacts/`.
