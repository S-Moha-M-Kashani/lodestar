# Changelog

Every entry below is a release point on `master` and carries the tag of the same
name. The ladder is the history: one landing, one release point, one tag, and
the version in `package.json` names the newest of them. An `Unreleased` heading,
when there is one, holds what has landed on `development` and is waiting for the
next release point — it becomes that version's entry when the point is built.

Dates are the day the release point was built. Versions follow
[semantic versioning](https://semver.org/) — a major number for a break, a
minor for a capability, a patch for a fix.

## [Unreleased]

**Importing onto a board that already holds cards, and three ways a card could
quietly lose part of itself in the JSON that travels between boards.**

**An import no longer duplicates what the board already has.** A card is its
words: same title, same notes, same thought. Re-importing yesterday's export —
or a file the Assistant built from the board it had just read — used to write a
second copy of every card, and the two copies then drifted apart under separate
ledger numbers. Now a card the board already holds unchanged is skipped, a card
it has never seen is added, and a card it holds in a different state — a
property changed, or different cards hanging off it — is **replaced**: the
file's version lands and the board's copy goes to the Trash, where it can be
brought back. A replacement never takes its children with it; they move to the
card that replaced their parent, so no import can lose a card it did not name.

**And you can walk through an import card by card.** Tick *Review each card
before it lands* and every card the file actually changes opens in the ordinary
card dialog on its way in, so it can be edited, accepted or refused one at a
time. Children come before their parents. A replacement is shown as one
sentence — the card on the board, an arrow, the card that would take its place —
and refusing it leaves the board's card exactly where it was. Every card you
accept is its own line in History, naming the card, so a reviewed import can be
unpicked one decision at a time; an import you did not review stays a single
line, because a 200-card file must not push the rest of your history off the end
of the log.

**A JSON backup now carries the whole card.** The `.json` twin written beside
every database snapshot — the recovery path that pastes straight into Import
JSON from any machine with a browser — had never been taught about the fields
the card gained after it was written. A board restored from one came back with
every hand-set plan collapsed back onto its deadline, every child detached from
its parent, and every finished card's date re-approximated from the last time
it was edited. The export now names `plan`, `planSrc`, `parentId` and `doneAt`
along with the rest, and the test that covers it asserts each of the four,
because a missing field here fails in silence until the day the backup is used.

**Adding an imported board keeps its cards together.** `Add to the board`
re-issues every card's id, so the same file can be imported twice without
colliding with itself — but a child still named the id its *file* had given its
parent, which nothing on the receiving board has. The link survived the import
dialog and was then dropped by the next save, so a card came apart from its
parent with nothing on screen to say so. The ids and the links are now
re-pointed together. Substituting the board keeps the file's own ids and was
never affected.

**The import dialog's schema says how to write those links by hand.** It
documented `parentId` without ever saying that a card may carry an `id` for
another card to name, and it did not mention `doneAt` at all, although the
export writes one. Both are listed now.

## [v1.9.0] — 2026-09-08

**Four changes: a card can be made of smaller cards, a habit says when each
repetition is due, every view can be asked what a span of time held, and the
card dialog is a ledger instead of a form.**

**A card can hold other cards.** "Plan a long weekend in the mountains" is six
or seven smaller acts, and the only way to hold them was more top-level cards
that drift apart or a checklist typed into the notes that nothing can count.
Any card can now hold children, and a child is a card in its own right that
names one parent — nothing is stamped and nothing is a "project". The link
lives on the child alone, so two machines editing one board can never disagree
about it, and deleting a parent leaves its children on the board rather than
taking them with it. A new scope filter narrows every view to parent cards, so
a piece of work is counted once; a text search still reaches every card, because
a control that narrows the board must never hide the thing you asked for by name.

**A habit says when, not only how often.** A habit that repeats three times a
week can now name a weekday and an hour for each of the three, and the fields a
cadence cannot use are shown greyed with the reason rather than removed. Every
reminder that existed before survives the change untouched. A habit is also
drawn in one place now — one line in the rail beside the board, boxes first and
the name after — instead of being both a rail row and a board card saying the
same thing twice.

**Every view can be asked what a span of time held.** A card records the day it
was finished, which the weekly review had been guessing from the last time the
card was edited — so a card finished in July and edited in September counted as
September's work. The board, the backlog, the map, the matrix, the areas and the
review can all be narrowed to what was captured or finished inside a range, and
the range is remembered between visits and stated back in words. Asking about
finished work stands the hide-Done default down for as long as the question is
being asked, so the answer is never an empty screen.

**The card dialog is one ledger of properties.** Every setting used to sit in
its own framed box with a shouted heading, and most of them were folded away
behind a Details flap so the lot would fit. Each one is now a single row — the
name of the property on the left, its value on the right — with no box drawn
around a control until you hover or click it. Nothing folds any more except the
list of child cards, so a setting can no longer hide from the person editing it.

**The map opens in half a second instead of nineteen.** Each card's meaning is
worked out once and kept in the database, and the drawing is done by the Python
service rather than by the browser, so a three-hundred-card board no longer
re-reads every card it has already read. A second visit costs a fifth of a
second and changing a filter costs milliseconds. The map still draws with the
service stopped, and says which way it is drawing.

**The rail beside the board is sorted.** The habits and the plan are short lists
grouped by how often or how soon, each named by a coloured strip down its left
edge instead of by a heading that ate the margin's height.

**Fixed.** Asking what you finished this month with the Done column switched off
kept the right cards and hid the one column that draws them, so the board came
back empty. A lineage line above a card's title was drawn on every card, naming
nothing.

## [v1.8.0] — 2026-09-06

**Four changes: the card's actions, where it is filed, what each view shows,
and how wide the rail is.**

**A card's actions follow it everywhere.** Right-clicking a card gave you its
actions menu on the Board and nowhere else, so re-filing — the commonest thing
anyone does to a card — was unreachable from the Backlog, the map, the Matrix,
the Areas and the Review. The same menu, with the same entries and the same
dismiss rules, now opens on a register row and on a plotted dot. Where a card
has no room for its own control the panel is built beside whatever was clicked
and taken away again on close, so no dead menus collect under the page.

**A card can be filed from the card.** Three chips above the type stamp, outside
the settings fold, say Inbox, In Progress and Done — where a card lives is the
question an open card is asking, not a setting. The move is part of the save, so
it is one entry in History rather than an edit and a move. In Progress is struck
through for a habit, which cannot live there. The Assistant's suggested moves
are painted into that control too, instead of being applied where no reviewer
could see them.

**A finished card stays in the Done column.** Everywhere but the Board a card in
Done is a record, not work, and it was appearing beside the open ones on every
view — so the map, the Matrix and the register all read as busier than the week
had been. A fourth filter beside type, priority and tags names the column to
show: Open cards, Inbox, In Progress, Done, or All columns. It is not painted on
the Board, which is already three columns, and the Menu can switch the whole
idea off — which takes the hide-Done default with it, because a rule narrowing
the board from behind a control that is gone is a board nobody can explain.

**The rail is as wide as you want it.** The habits-and-plan rail was a fixed
200px: a long habit name clipped on a narrow window while a wide one gave the
cards room the rail could have used. The divider between the columns and the
rail is now a real handle — drag it, or focus it and use the arrow keys, and the
width is remembered. What the rail gives up is shared equally between Inbox, In
Progress and Done, so the three columns always match. Hiding the Done column
closes the board up behind it as well; its empty slot used to keep a full
column's width and leave the habits looking adrift.

Dependency updates ride along below. `v1.7.8` and `v1.7.9` were built for these
alone on 2026-09-05 and taken back the next day: a version number should name
something a reader gets.

**The agent framework steps up.** `langchain` to 1.4.0, carrying
`langchain-core` 1.6.2 and `langgraph` 1.2.11; `langchain-openai` to 1.6.0; and
`numba` to 0.67.0 in the `voice` extra, with `llvmlite` 0.49 behind it.

Taken one at a time, each on its own branch with the full suite between them,
because a framework the whole agent sits on is not something to move in a batch.
Both framework bumps were also checked against a **live** tool-calling turn —
the agent called `find_related`, answered from real cards, and reported a real
cost — since the suites run on a deterministic fake model and would not notice
an adapter that had stopped reporting usage. The `numba` bump needed the `voice`
extra installed to be exercised at all; nothing in the ordinary test run touches
it.

**The brain's dependencies move forward.** `fastapi` to 0.141.1, `uvicorn` to
0.52.4, `ddgs` to 9.16.0, and `sentence-transformers` to 6.0.1 in the
`local-embeddings` extra. The `ddgs` update also sheds eight packages it no
longer needs, so a full install is smaller than it was.

Taken together rather than one at a time, because all four touched the same two
files: the lockfile was regenerated once over the merged constraints. Verified
past the test suites — both services were started on the real configuration and
answered a live turn end to end, which no test does, since the suites run on a
deterministic fake model by design.

## [v1.7.7] — 2026-09-05

**The files a stranger looks for.** Nothing in the app changed; everything a
visitor reads before the app did.

- This changelog, one entry per release point, in the tag's own words.
- `SECURITY.md` at the root, where GitHub looks for it, saying what the boundary
  defends, what the injection measurement actually is, and where to report a
  hole. The long version stays in `docs/security.md`.
- `package.json` names its licence, author, repository and homepage, so a tool
  reading it no longer thinks the project is unlicensed and homeless.
- Forms for the two kinds of issue this licence can accept — a bug, and a claim
  in the documentation that is wrong — and a pull request template saying why
  there is no third one.
- Dependabot watches the CI actions weekly. A retired action is what turned
  every run red at v1.7.2 and v1.7.3, and it is the one breakage nothing in the
  tree can see coming.
- `--anyway` on the release script, for a release the commit types cannot see.
  This one.

## [v1.7.6] — 2026-09-05

**A board switch the test suite waits out.** Switching boards reloads the page,
and the end-to-end suite went looking for the new board's cards in the document
the reload was about to replace.

## [v1.7.5] — 2026-09-05

**The key you typed is the key that is saved.** An OpenRouter key typed into the
Assistant's settings was lost when the panel repainted underneath it.

## [v1.7.4] — 2026-09-05

**The search box beside the button it belongs to.** Capture and search now share
one row at the head of the Inbox, and the row fills the width it is given.

## [v1.7.3] — 2026-09-05

**An action reference the runner can resolve.** CI named a version of a build
step that does not exist, so every run failed before it tested anything.

## [v1.7.2] — 2026-09-05

**The last red check was the check, not the app.** An end-to-end assertion read
the composer instead of the turn, so it declared a reply finished before it was.
The build steps also moved off a retired Node version.

## [v1.7.1] — 2026-09-05

**A publication check that works from any checkout.** The private-data gate read
`master` by a name only this machine has, and CI hands the runner a checkout
where that name is spelled differently.

## [v1.7.0] — 2026-09-05

**One row to capture and search, and a CI run you can believe.**

- `+ New card` and the text search merge into a single row at the head of the
  Inbox and the Backlog.
- Habits are refused by the In Progress column — a habit's progress is already
  the punch strip in the rail.
- The habits-and-plan rail docks to the window's edge instead of floating in
  the middle of a wide screen.
- An `Ask` pill in the corner opens the Assistant widget from any view.
- The password may live in `.env` in plain (`LODESTAR_AUTH_PASSWORD`) as well as
  pre-hashed, and the server reads that file itself at boot.
- Docker Compose can publish the board on one named home address, deliberately
  not on every interface.
- CI is given the history and tags its own checks read, and the README reports
  the run, the licence and the Node version as badges.

## [v1.6.0] — 2026-09-03

**Capture cards deliberately, and show how Lodestar is built.**

- The Inbox's quick-add form is replaced by `+ New card`, which opens the card
  dialog on a draft — nothing is written and no ledger number is spent until
  you save. Right-clicking a card offers `Duplicate` into the same dialog.
- A suggested edit no longer drops the due date it named.
- Editing one card of sixty asks the encoder for one embedding instead of sixty.
- A tool's own model call is no longer streamed into the transcript as if the
  assistant had said it.
- Another board's chat can no longer arrive through the dense half of a recall.
- `ARCHITECTURE.md`, a screenshot gallery, and the design records renamed and
  dated.

## [v1.5.1] — 2026-09-02

**The publication check no longer trips over itself.** The check that keeps
private names out of a release was finding the stand-in name its own test had
written down, and refusing the release because of it. The stand-in is now made
fresh on each run and never written into a file.

## [v1.5.0] — 2026-09-02

**The board is yours alone, and the repository proves it.** The board now
listens only on this machine and asks for a password before it shows a card, so
a shared network cannot read a private life. Publishing is gated too: a check
walks every saved version a push would make public and refuses to release when
it finds a database or a real person's name.

## [v1.4.0] — 2026-08-28

**A plan date on every card.** Plan stops being a kind of card and becomes a
date any card can carry, with a rail that groups the planned cards by how near
they are.

## [v1.3.0] — 2026-08-28

**The server owns the board.** A save names the version it was written against,
and one that is behind may add but never delete. Fixes the loss of 24 cards to a
second machine's stale copy.

## [v1.2.0] — 2026-08-20

**Several boards, and a frontend in modules.** One database holds many boards,
each with its own cards, trash and chats. The 6,400-line frontend becomes forty
modules, and an Asana export imports as it comes.

## [v1.1.0] — 2026-08-06

**Chat becomes a conversation.** Chats have beginnings, a history panel, and a
nudge when a message has clearly changed the subject.

## [v1.0.0] — 2026-08-01

**The first complete release.** The card model settles: a card is a question,
problem, task, idea, plan or habit. Retrieval is rebuilt on hybrid search with a
relevance gate, and every step the assistant takes is visible as it happens.

## [v0.9.0] — 2026-07-29

**The assistant asks before it writes.** A card it invents is stored but
invisible until you approve it.

## [v0.8.0] — 2026-07-27

**Tested, backed up, and scheduled.** Three layers of tests, automatic database
backups, deadlines with a derived priority, and a visible model choice.

## [v0.7.0] — 2026-07-24

**The whole-life dashboard.** Life areas as colour, the Areas and Review views,
four matrix lenses.

## [v0.6.0] — 2026-07-23

**The assistant.** A separate Python service that can search the web, read the
board and find related cards.

## [v0.5.0] — 2026-07-23

**Runs anywhere, and becomes Lodestar.** Docker with a lasting volume, and the
name it keeps.

## [v0.4.0] — 2026-07-23

**Judgement, two new views, and a Trash.** Importance and urgency, the Overview
map, the Eisenhower matrix, and deletion that takes two deliberate steps.

## [v0.3.0] — 2026-07-23

**The board outlives the browser.** A dependency-free Node server stores one row
per card.

## [v0.2.0] — 2026-07-22

**Import, export, undo and themes.** Every change is a snapshot you can step
back through, and the board travels as a JSON file.

## [v0.1.0] — 2026-07-21

**The board exists.** Three columns you drag cards between, saved in the
browser.

[v1.9.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.8.0...v1.9.0
[v1.8.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.7...v1.8.0
[v1.7.7]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.6...v1.7.7
[v1.7.6]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.5...v1.7.6
[v1.7.5]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.4...v1.7.5
[v1.7.4]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.3...v1.7.4
[v1.7.3]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.2...v1.7.3
[v1.7.2]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.1...v1.7.2
[v1.7.1]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.7.0...v1.7.1
[v1.7.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.6.0...v1.7.0
[v1.6.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.5.1...v1.6.0
[v1.5.1]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.5.0...v1.5.1
[v1.5.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.4.0...v1.5.0
[v1.4.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.3.0...v1.4.0
[v1.3.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.2.0...v1.3.0
[v1.2.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.1.0...v1.2.0
[v1.1.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.0.0...v1.1.0
[v1.0.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.9.0...v1.0.0
[v0.9.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.8.0...v0.9.0
[v0.8.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.7.0...v0.8.0
[v0.7.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.6.0...v0.7.0
[v0.6.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.5.0...v0.6.0
[v0.5.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.4.0...v0.5.0
[v0.4.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.3.0...v0.4.0
[v0.3.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.2.0...v0.3.0
[v0.2.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/S-Moha-M-Kashani/lodestar/releases/tag/v0.1.0
