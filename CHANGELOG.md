# Changelog

Every entry below is a release point on `master` and carries the tag of the same
name. The ladder is the history: one landing, one release point, one tag, and
the version in `package.json` names the newest of them. An `Unreleased` heading,
when there is one, holds what has landed on `development` and is waiting for the
next release point — it becomes that version's entry when the point is built.

Dates are the day the release point was built. Versions follow
[semantic versioning](https://semver.org/) — a major number for a break, a
minor for a capability, a patch for a fix.

## [2.1.0] — 2026-09-20

**Priority, effort and control are one row on the card, called Compass, and the
plan date can be pressed on a calendar.**

Three rows on the card ledger used to ask one question in three places — where
does this card stand — so weighing a card meant reading down the list to
collect the answer. They are one row now, **Compass**, laid out two by two: the
pair that places the card on the Matrix, then the pair that says what reaching
it would cost. Nothing underneath changed — the same four selects, the same
ids, the same values — only where they stand and how their options read.

The plan — a partial date, a year, a month, or a day you mean to get to
something — could only be set from three dropdowns. It can now be pressed on a
calendar instead: a grid gives the day, and pressing **Clear the day** or
**Clear the month** hands back precision rather than losing the date outright.
The three dropdowns are still there and still the only thing that writes the
plan; the calendar is a second way to press them; a picker, not a fold, shut on
every visit.

The ledger's own rows were redrawn alongside this: a hairline instead of a
dotted rule between them, a wider label gutter, and Where/Type drawn as the
same small stamped mark rather than one tilted badge and one plain box.

## [2.0.0] — 2026-09-20

**The header collapses to three destinations and a dropdown, every filter lives
behind one panel, the habit and plan rail read as one plain list, and a
calendar replaces the two date boxes.**

**The header row now holds three destinations and one dropdown.** Board,
Backlog and Review keep their own buttons; Overview, Matrix and Areas — the
three views that read the same cards a different way — move behind a
`Lenses ▾` dropdown, and the Assistant is reached from the corner `Ask` pill
alone. Seven tabs on one line put Review and the Assistant off the edge of a
phone screen where nothing could reach them; three plus a dropdown fits, and
scrolls sideways rather than wrapping onto a second line at any width.

**Filtering is one button, not a wall of controls.** Type, priority, tags,
where a card is filed, when it's due, and whether a child shows up on its
own — six dropdowns that used to stand above every column, most of them
switched off and saying nothing — are now one `+ Filter` panel listing what
the board can be narrowed by, each opening its own page. Nothing underneath
was renamed: the same controls fire the same listeners they always did, only
parked out of sight until asked for. A date range is now picked on a
calendar, in two clicks, instead of typed into two boxes that could quietly
hold a contradiction — a start after its end. And the plan list beside the
board now follows every filter, exactly as the board's own cards do; it used
to sit outside them behind its own switch, so a board narrowed to one
question still carried a plan answering a different one.

**The habit and plan rail reads as one plain list.** It was four habit groups
and six plan sections, each behind its own coloured spine naming a cadence or
a date span every row already said in its own tooltip — height spent, in the
one panel whose whole job is to be narrow. The redraw trades that structure
for plain rows: smaller, rounder tick boxes that fill with the card's own
life-area colour when checked, quiet monospace headings in place of bold
ones, and a plan row that now reads its date before its title.

**Your Codex or Claude subscription can answer the Assistant in Docker, and you
sign it in by pressing a button.** Both were already offered when Lodestar runs
from your own account; in a container neither worked, because the brain's image
carried neither program and had nobody's login in it. The image carries both
now, the composed brain answers through **Codex** by default, and the
Assistant's ⚙ settings grew a **Your subscriptions** block.

Codex signs itself in from there: press *Sign in*, and the row turns into a link
and a one-time code. Open the link on any device — your phone will do — type the
code, and the row says so by itself. The container needs no browser of its own,
no port opened to it, and no credential carried in from your machine: the login
it earns is its own, kept in Docker's own storage where neither the Google Drive
backup nor git can reach it, and it survives a rebuild.

Claude cannot work that way and the reason is not Lodestar's: its login is a
terminal program, and on a Mac what it writes is a Keychain entry no container
can read. So that row takes a token instead (`claude setup-token`), stored the
way the OpenRouter key already is — the brain keeps it, the field empties
itself, and no page ever reads it back. A subscription Lodestar cannot check on
says *cannot be checked from here* rather than guessing that you are signed out.

One cost, stated rather than left to be discovered: a freshly built stack cannot
answer a single turn until that login is done once. Running it in the container
is what showed how that failure actually looks — Codex reports `401 Unauthorized:
Missing bearer`, which is true and leaves you nowhere, and it **exits as though
the turn had succeeded**, so the reply arrived blank. A failed turn is now an
error, and the error names the cure: sign the subscription in from the gear.

**The board is reachable from the editor, over MCP.** An assistant that lives
where you work — Claude Code in a terminal, Claude Desktop — can now read, filter,
write, change and delete cards without a browser tab. Eight tools: the board's
own vocabulary of life areas and tags, a search with every filter the board can
express (keyword, column, type, area, importance, urgency, effort, control,
tags, lineage, and date spans over when a card was captured, finished, updated,
due or planned), one card in full, and the four that change something.

It is a client of the board's own web interface and never of the database, so
the running board stays the one thing that writes, and every save is the whole
board the way a browser saves it — one edit can never archive the cards it did
not mention. **It cannot destroy a card**: deleting moves it to the Trash, which
the same tools can list and undo, and "Delete permanently" stays on the board
itself. Unlike the Assistant's own agent, these tools write straight to the
board, because you are driving them and your client shows you each call before
it runs.

Two failures were found by running it against a real board rather than by
asking a test for them, and both are fixed here. A new card was taking a ledger
number that a card in the Trash still held, so restoring that card would have
left two cards reading `C-395`; the number now counts the Trash. And a save that
arrived while another tab was saving was retried in a way that wrote the new
card twice — the retry now produces the same board however often it runs.

**The board can be asked a question instead of built out of parts.** Five named
views stand above the filters — Today, This week, Everything open, Waiting on
me, Finished — and pressing one sets the filters the app already applies, so
nothing new decides which cards you see. Which one is pressed is worked out from
the filters themselves rather than remembered, so moving any control by hand
quietly un-presses the row instead of leaving a button lit over a board it no
longer describes, and a board nobody has narrowed reads as "Everything open",
because that is what it is. A pressed view leaves one chip that says its name
and hands the board back when you close it.

One new thing came with it and is worth naming on its own: the board can now be
filtered by **when something is due**. It could only ever be asked when a card
was written down or when it was finished, so "what is due this week" — the
question the two commonest views are made of — had no control at all. It is in
the filter panel too, as a Due option with an "Up to today" span that includes
what is already late.

**A few surfaces on the phone were still sized for a mouse.** The board itself
had already been measured and fixed; this pass opened the card dialog,
Settings, the Menu and the filter panel and found the same problem waiting one
level in — a deadline field at 13px, a model picker at 11.5px, a Menu row at
30px, a Settings picker at 24px. Every typeable field on every one of those
surfaces is now at least 40px, or 16px for anything you type into, which is
also what stops the phone zooming in on focus and never zooming back out.

**The ⚙ Menu is eight rows now, not roughly twenty behind three hover
submenus.** New, Rename and Delete board, History, Export JSON, Import JSON,
Remove duplicates, and `Settings…` — an action you take now stays in the
Menu, and a preference you set once a year moves to the new Settings dialog,
laid out as one property list exactly like the card dialog's own: theme, plan
layout, habit sound, chime, and the four Show switches, each a labelled row
instead of a hidden flyout. Nothing about the controls changed, only where
they live. Alongside it, a board full of tags gained a way to find one by
name: a small search box narrows the tag dropdown and the tag bar as you
type, without touching a single card.

**The card says four things, and the board says what it's asking for.** A
card on the board used to paint ten small marks before it painted a word of
its own; now it says its life area, its own words, one line of marks, and
its ledger number, with the rest — the type's glyph, its priority, its tags,
the raw deadline — moved to a tooltip, the Matrix or the dialog, wherever
that fact is actually being compared. Above the three columns, a new line
reads the board's own state in plain words — what's overdue, a habit falling
behind, what's due soon, a plan naming right now, an area nothing has moved
in for a month — worked out from the cards alone, with no model asked.

**Two changes: the board is reachable from a phone anywhere in the world, and
it is finally laid out for one.**

**The phone reaches the board over the private network.** Tailscale answers for
this machine's tailnet name and hands each request to the board's own loopback
port, so nothing new is published to any network and the trust boundary does not
move. The session is encrypted end to end, the password is unchanged as the
gate, and a device that is not on the network cannot find the door at all. One
Host-allowlist entry is what the board needed to stop refusing that name, and a
configuration test pins it, because the security notes plan the edit that
removes its neighbours.

**And the app is laid out for the screen it is now opened on.** Measured at
390 x 844 before: the page was 800px wide inside a 390px window, so the whole
board slid sideways under the thumb; the seven view tabs stood on one line that
put Review and Assistant off the right edge where no tap could reach them; the
category tabs stacked one per line; about 1,150px of that chrome stood in front
of the first card on an 844px screen; every control was sized for a mouse; and
every text field was small enough to make the phone zoom in on focus and never
zoom back. Now the page never scrolls sideways, every view is reachable, the
categories and the filters each keep one swipeable line, the controls are 40px,
and the cards start 248px down. It is the same app and the same stylesheet —
no framework, no second layout, no second codebase — and the desktop board is
untouched.

**It can be added to the home screen.** Added there it opens in its own window
with no address bar, which is worth real height on a board that is one window
tall, and the tile carries the nova from the app's own header — rendered from
that same drawing rather than drawn a second time. Installing is done from the
board while logged in: the icons and the description file sit behind the session
like every other file of the app, and the door stays the only public page.

**Two board owner's actions: remove duplicate cards, and empty this board's
undo history.** A card is its words — title and notes, trimmed and
lower-cased — and two cards sharing them are duplicates whatever else about
them differs. `Remove duplicates` keeps the copy furthest along toward Done,
re-points any child or duplicate-habit onto the survivor, and retires the
rest to the Trash in one undo-able save; `Empty history` resets the undo log
to the board as it stands right now, destroying no card, for the times the
log has grown long enough that "how the board got here" is no longer
interesting.

**The Assistant opens on the board's own reading of things, not an empty box
and eight suggestions.** A widget that used to greet you with a blank
composer now names up to five cards — overdue, a habit behind its cadence,
due soon, a hand-set plan naming now, a quiet area — worked out from the
cards alone by a rule with no score and no model call behind it. Opening it
now costs a handful of requests instead of the dozen it used to, because
reading the board is not asking the Assistant anything.

**The second assistant, "Widget 2", answers by default now, with three tools
of its own.** It shipped with none at all; it now finds cards with one call
covering every filter the board can express, proposes new ones, and suggests
edits to existing ones — the same confirm-before-anything-changes gate the
first assistant already uses, so it still has no way to write to the board
on its own. Which one answers is a setting the composed stack can now make
too, and two small bugs went with the change: a suggested edit was reaching
the tools without the chat it was meant to answer inside, and a plan date
proposed or suggested for a card was silently dropped rather than saved.

**And a fix for anyone running the brain in Docker:** the model it uses to
understand card text is now cached on the machine rather than the container,
so rebuilding the stack costs that multi-gigabyte download once per machine,
not once per rebuild.

## [v1.9.0] — 2026-09-08

**Six changes: a card can be made of smaller cards, a habit says when each
repetition is due, every view can be asked what a span of time held, the card
dialog is a ledger instead of a form, an import no longer duplicates what the
board already has, and a JSON backup carries the whole card.**

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

[v2.1.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/S-Moha-M-Kashani/lodestar/compare/v1.9.0...v2.0.0
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
