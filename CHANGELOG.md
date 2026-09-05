# Changelog

Every entry below is a release point on `master` and carries the tag of the same
name. The ladder is the history: one landing, one release point, one tag, and
the version in `package.json` names the newest of them.

Dates are the day the release point was built. Versions follow
[semantic versioning](https://semver.org/) — a major number for a break, a
minor for a capability, a patch for a fix.

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
