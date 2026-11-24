# Screenshots

Every image here is a real, unedited screenshot of the app. None shows real
personal data: the card text comes from the demo board in
[`sample-overview.json`](../../sample-overview.json), plus a few synthetic
deadlines, plans, sub-cards and habits added by the capture script.

## Regenerating

```sh
uv run --with playwright python scripts/capture-screenshots.py docs/img
```

The script starts its own server on port 3017 with a temporary database, so
your own board is never read or changed. It rewrites `hero`, `filters`, `card`,
`overview`, `matrix`, `areas`, `review` and `mobile`. Desktop shots are taken at
1440×900; the phone shot at 390 px wide, 2× scale.

## Files

| File | Shows | Source |
| --- | --- | --- |
| `hero.png` | The Board: columns, summary line, habits and plan rail | capture script |
| `filters.png` | The filter window | capture script |
| `card.png` | The card editor on a card with sub-cards | capture script |
| `overview.png` | Cards mapped by meaning | capture script |
| `matrix.png` | Eisenhower matrix with the Unknown corner | capture script |
| `areas.png` | Life-area radar and tiles | capture script |
| `review.png` | Weekly review | capture script |
| `mobile.png` | The board on a phone | capture script |
| `assistant.png` | The assistant proposing a card for approval | recorded live, 2026-09-02 |
| `approval-gate.gif` | Proposal → Approve → card on the board | recorded live, 2026-09-02 |
| `board-habits.png`, `history.png`, `categories.png` | Habits, undo history, life-area editor | recorded 2026-09-02; not used by the README |

File names are referenced from the README — add new files rather than renaming.
