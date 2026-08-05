# Chat sessions — one conversation at a time

Date: 2026-08-04 · Branch: `feat/natural-chat`

## What this is

The Assistant has no notion of a conversation. There is one endless transcript,
and every turn carries a slice of it, so saying *"hi"* after three weeks of use
produces an answer about whatever you were working on last month. This adds
**sessions**: a chat has a beginning, a title, and a boundary the model cannot
see past. A **New chat** button opens one. A **history panel** lists the old
ones and reopens any of them, still live.

It serves the "give direction" pillar from the other side: an assistant that
answers the question you did not ask is worse than no assistant, because you now
have to argue with it.

## The bug, precisely

Three separate causes, only one of which is about window size.

1. **`app.js:2882` pins the first message you ever sent.** `contextWindow` ends
   with `history.find((m) => m.role === 'user')` and prepends it *outside* the
   character budget, on purpose, as "framing". With one endless transcript that
   framing message is not the subject of this conversation — it is the subject of
   your first conversation, permanently stapled to the top of every request for
   the life of the board. This is the dominant cause and it is deleted here.
2. **The 16-message window straddles topics.** `CONTEXT_MESSAGES = 16` is a
   rolling count over a transcript with no seams, so a fresh question arrives as
   `[first-message-ever, …15 messages of the previous subject…, "hi"]`. A model
   answering the previous subject is reading that input *correctly*.
3. **`assistant.db` has no session column** (`server.js:317`) — one flat
   append-only `messages` table — so nothing downstream *could* scope itself:
   not the window, not `recall_chat`, not the UI.

Nothing here is a model failure, and no amount of prompt work fixes it. The
prompt already says the conversation may be a window; the window is simply
pointed at the wrong thing.

## Decisions taken

Four questions were put to the user; these are the answers this spec implements.

| Question | Decision |
| --- | --- |
| What can a new chat reach from older chats? | **Nothing automatically.** Older chats stay searchable through `recall_chat`, which fires only when the user refers back. |
| Where does chat history live? | **Everything in `assistant.db`** — messages, tool steps, usage and cost. A browser-data wipe loses nothing. |
| What do you see when the Assistant opens? | **Resume the last chat, unless the gap was long**, then a fresh one. |
| How is a topic change handled? | **Detected and offered**, never applied silently. |

The fourth was chosen against this document's recommendation, which was the
session boundary alone. The recommendation is recorded in *Alternatives
considered* rather than removed, because the nudge's thresholds are the part
most likely to need revisiting and the reader should know what the cheaper
option was.

## Data model

A new table, and four columns on `messages`. Added by the boot-time
`PRAGMA table_info` migration `server.js` already runs for `cards`, so no
migration framework enters a server whose whole point is zero dependencies.

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,   -- crypto.randomUUID(), minted by the browser
  title      TEXT    NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER
);
```

| New `messages` column | Type | Rule |
| --- | --- | --- |
| `session_id` | `TEXT NOT NULL DEFAULT ''` | never empty after the migration below |
| `steps` | `TEXT NOT NULL DEFAULT '[]'` | JSON array, assistant rows only |
| `usage` | `TEXT` | JSON object or NULL — **NULL is a fact**, see below |
| `cost` | `REAL` | NULL when the price was unknown |

`usage` and `cost` are nullable and stay nullable. `pricing.py` already refuses
to fabricate a zero, and a turn stored as `cost = 0` would be a measurement
nobody made — the same reasoning that keeps the Assistant from implying a paid
turn was free.

`sources` is deliberately **not** stored. `sourcesOf(steps)` (`app.js:4015`)
derives it, so storing it would be a second copy of a fact that can go stale
against the first.

### Titles are derived, not generated

The title is the session's first user message: first line, trimmed, 60
characters. Set server-side when the session row is created, so one place
decides it and an import cannot arrive untitled. Renameable afterwards.

No LLM call. A generated title costs a request and a wait on every new chat to
improve on text the user just wrote, and it is the one place a model's
paraphrase would sit permanently in the furniture.

### Migration of the existing record

The same boot migration mints one session, titles it `Earlier conversations`,
and adopts every message that has no `session_id`. Two consequences worth
stating: nothing existing is orphaned, and `session_id` is never empty after
boot, so no read path needs a NULL branch.

## Who writes what

**The brain stays the sole recorder of a turn.** `remember()` in
`brain/src/lodestar_brain/server.py` is right that "a second route is a second
place to forget", and this change does not add a second writer.

- `ChatBody` gains `session_id: str`.
- `remember()` passes it to `BoardClient.record_chat`, along with the assistant
  row's `steps`, `usage` and `cost` — all three of which it already holds.
- The browser sends a session id and **never writes a message**. Session rows are
  upserted by the message POST, so there is no "create session" call that a code
  path could forget to make.
- The browser's only writes are rename and delete.

**Known gap, stated rather than papered over.** A turn that dies before the model
answers is not recorded: the route raises, `remember()` never runs. The question
survives on screen and in the composer. This is today's behaviour unchanged — it
is written down here so the next reader does not discover it as a surprise.

## API

All on the Node server, alongside the existing `/api/chat/messages`.

```
GET    /api/chat/sessions       -> {sessions:[{id,title,createdAt,updatedAt,messageCount}]}
                                   live only, newest updatedAt first
GET    /api/chat/sessions/:id   -> {session, messages:[{id,role,content,createdAt,
                                   steps,usage,cost}]}
PATCH  /api/chat/sessions/:id   <- {title}            rename; empty title refuses
DELETE /api/chat/sessions/:id   -> soft delete: sets sessions.deleted_at
POST   /api/chat/messages       <- gains sessionId (optional); optional per-row
                                   steps / usage / cost
DELETE /api/chat/messages/:id   -> soft delete of ONE turn: sets messages.deleted_at
GET    /api/chat/trash          -> {messages:[{...,deletedAt,sessionTitle}]}
                                   turns deleted one at a time, newest first
POST   /api/chat/trash/:id/restore -> clears deleted_at
DELETE /api/chat/trash/:id      -> THE hard delete: removes the row
```

### Deleting one message (added 2026-08-06)

A whole chat could be deleted; one sentence inside it could not. So a pasted card
number, a misdictated line, or an answer that quoted something private stayed in
the record and in recall for good, and the only way out was to delete the whole
conversation around it. The `×` on a turn is the missing granularity.

It is the board's two-step, applied to chat, and the shape is the point: hiding
and destroying are **different calls**. `DELETE /api/chat/messages/:id` stamps the
row; the turn leaves every live read and lands in **Deleted messages** at the foot
of the chats panel, carrying the title of the chat it came out of, because a
sentence out of its conversation is unplaceable. From there, Restore or Delete
permanently — and the purge requires `deleted_at IS NOT NULL`, so a live turn can
never be erased by one call.

This gives chat its first hard delete, which the section above deliberately ruled
out. The promise it kept was that no *single* call could destroy a message; that
promise is intact. What has changed is that the user can now finish the job
themselves, which is what the durability pillar is actually for — never losing a
thought is not the same as never being allowed to take one back.

Two rules fall out of the design rather than out of taste:

- **`GET /api/chat/trash` excludes the messages of a deleted chat.** There the
  chat is the unit. Listing its turns loose would bury real deletions under a
  whole transcript, and restoring one into a chat that cannot be opened would be
  a restore with nothing to show for it.
- **Both actions fire `/api/rag/chat/reindex`**, because the index has to follow
  the record in *both* directions now: `prune` takes a hidden turn out of recall,
  and `sync` — which only ever adds — is exactly what a restore needs.

The client-side cost is one thing the transcript never carried: the record's row
id. `restoredMessage` keeps it, and a settled turn learns it by aligning the chat
on screen against the chat in the record on **role and text**. Not on position:
the browser appends a turn when it is spoken and the brain records it once the
model has answered, so the two lists differ by exactly the turns that failed, and
a positional pairing would slide by one at the first error bubble — the `×` would
then erase a different message than the one it sits on. A turn that matches
nothing has no id and therefore no delete control, which is honest: there is
nothing in the record to delete.

### Why `sessionId` is optional rather than required

Requiring it reads better and was the first draft. It was dropped after counting
the callers: sixteen brain tests, the evals and any curl POST to `/agent/chat`
would all have to name a session to record a turn, and `ChatBody.session_id`
would become a required field on a route whose only real caller is the browser.
The churn would be large and the diff would stop being about sessions.

So a message POST without `sessionId` lands in one reserved session,
`id = 'adhoc'`, titled **Unsessioned (API)**. Nothing is lost, and non-browser
turns are *visibly* separate in the history panel rather than mixed into a real
conversation. `ChatBody.session_id` defaults to `''` and is forwarded only when
non-empty.

This is a default for an unspecified request field, not a backend chosen behind
the caller's back — the "no auto modes" rule governs seams (which embedder, which
model), and does not apply here.

The one caller that must never use the fallback is **import**: `importChatFile`
mints a session of its own, titled from the export's own first message, so a
imported transcript arrives as a chat you can open rather than as loose rows.

`DELETE` is a deliberate relaxation of a documented promise: `tests/chat.test.js`
currently records that "there are deliberately NO delete routes" for chat. It
becomes a **soft** delete only — the messages are untouched and no hard-delete
route exists for chat at all, so the durability promise holds. That comment is
updated in the same change, because a stale comment claiming a stronger promise
than the code keeps is worse than no comment. (The no-hard-delete half of that
was relaxed in turn on 2026-08-06 — see *Deleting one message* above, which gives
chat one purge, behind the same two-step the board has always had.)

`messageCount` is counted in SQL rather than by loading transcripts: the history
panel lists every chat, and reading every message to render a list is how a list
becomes slow at exactly the point the feature becomes useful.

### Deleting a chat has to reach the index too

A soft-deleted session leaves `GET /api/chat/sessions` **and** its messages leave
`GET /api/chat/messages` — the same semantics an archived card has, where the row
survives but the live read stops returning it.

That is not enough on its own. Chroma holds chunks derived from those messages,
and `ChatStore.sync()` only ever *adds*, so a deleted chat would keep answering
`recall_chat` — a chat you deleted resurfacing in an answer is the worst possible
version of this feature. So `ChatStore` gains a counterpart:

```python
def prune(self, rows: list[dict]) -> int:
    """Drop chunks whose message_id is no longer in the live record."""
```

called wherever `sync` is called (boot, and `POST /rag/chat/reindex`), and the
browser fires the existing reindex route once after a delete so the effect is
immediate rather than waiting for the next boot. `prune` is worth having beyond
this feature: it is the missing half of a derived index, and without it *any*
future message removal leaks.

No hard delete for chat exists after this change, exactly as before.

## Context — what one turn carries

`contextWindow` loses the framing injection entirely. A request carries **this
session's replayable messages**, trimmed from the oldest only when they overrun
the existing budgets (`CONTEXT_MESSAGES = 16`, `CONTEXT_CHARS = 24_000`). Nothing
is pinned outside the budget.

Most chats never reach the trim, so the ordinary case is that the model sees one
conversation, whole, and `"hi"` in a new chat is a request containing exactly one
message. The `replayable` predicate is unchanged: errors and abandoned partials
are still rendered and never replayed.

The brain's `MAX_MESSAGES` / `MAX_CHARS` refusal is untouched and remains the
backstop for callers that are not the browser. `tests/context.test.js` keeps
pinning the ordering between the three numbers.

## The topic-change nudge

A new module, `brain/src/lodestar_brain/topics.py`, behind
`POST /agent/topic-check`, called by the browser **before** a turn is sent.

Two signals, cheapest first:

1. **A bare opener.** `"hi"`, `"hello"`, `"سلام"` — a greeting carrying no
   subject. Free, local, pattern-based, and it is the user's own first example.
   Modelled on `signals_no_audio` (`voice/base.py`) down to its false-positive
   reasoning: the patterns stay tight and mostly start-anchored, because a wrong
   nudge costs one click while a missed one costs nothing but the status quo.
2. **Semantic distance.** Cosine between the incoming message and the centroid of
   this session's recent user messages, through the existing `make_embeddings`
   seam. No new dependency, no API spend on the local embedder, and deterministic
   under an injected stub.

The verdict is a dataclass — `DriftVerdict(changed: bool, score: float,
reason: str)` — not a bare boolean. `reason` is what lets the UI say *why* it
asked and lets a failed calibration run be read.

**It never splits the chat by itself.** When it fires, the turn is not sent: a
strip appears above the composer — *"This looks like a new subject"* with
**Start a new chat** and **Keep this one** — and the text stays in the composer.
Accepting opens a fresh session and sends the message there; dismissing
suppresses the nudge for the rest of that session, so it cannot nag a
deliberately broad conversation.

Not sending the turn is what makes this cheap and reversible. The alternative —
answer first, offer to move the message afterwards — has to either re-run the
turn against a different context or leave the answer in the wrong chat.

### The threshold is calibrated, not invented

It ships provisional and a labelled fixture set decides it: same-topic pairs,
different-topic pairs, and bare openers, scored against the real embedder as an
eval under `brain/tests/evals/`. Retrieval questions in this repo are settled by
measurement; a distance cut-off is a retrieval question.

Failure is open, not closed: if `/agent/topic-check` errors or the embedder is
unavailable, the turn sends normally. A broken detector must not stand between
the user and their own assistant.

### It costs a second trip through the rate limiter

`/agent/topic-check` sits under `/api/agent/*`, so the token bucket in
`server.js` meters it — a pre-send check makes every message spend **two**
tokens of `LODESTAR_AGENT_BURST` (60) rather than one. That halves the effective
burst, which is still far above a human typing, so the knob is left alone. It is
recorded because the next person to widen the assistant surface should know the
budget is no longer one token per turn. The check is skipped entirely on the
first message of a session — there is nothing to drift from — which is also the
case where a new chat would otherwise pay for a check that can only say "no".

## UI

The Assistant view **already tried a rail and removed it** (`app.js:3495`): "a
rail beside the transcript cost it 300px of width and stood mostly empty". So
history is not a permanent sidebar. The established pattern is a button in the
toolbar row opening a panel across the sheet, same wiring as the ⚙ extras panel
and the board's Menu.

- **`+ New chat`** in the toolbar row.
- **The current chat's title, as a button**, opening the history panel. It
  doubles as the label for where you are.
- **The panel** lists chats grouped `Today` / `Yesterday` / date, with message
  count; rename and delete sit per row. Clicking one **opens it and keeps
  talking in it** — historic chats are live, not read-only.

New class names only, nothing renamed (CSS class names are test-stable API):
`.chat-history`, `.chat-history-item`, `.chat-drift`, `#chat-new`.

**On open: resume unless the gap was long.** One named constant,
`RESUME_WITHIN_MS` (provisionally 4 hours), compared against the session's
`updated_at`. An empty new chat is never persisted until its first message, so
glancing at the Board and coming back cannot litter the list — which is also
what makes the New chat button safe to press twice.

`localStorage` shrinks to the current session id and the draft; the transcript
now comes from the database. The flat `lodestar:chat` key is dropped rather than
migrated — the DB migration already holds that text, and the only thing the key
has that the record does not is error bubbles and abandoned partials, which are
decoration rather than the user's thoughts.

## Recall, so a new chat stays clean

`recall_chat` keeps searching every chat, per the decision above, with two
changes:

- **The current session is excluded.** It is already in the context window;
  returning it spends the model's attention re-reading what it can already see.
- Hits are labelled with their **date**, which `created_day` already carries.

Labelling hits with the chat's *title* was designed and then cut. The title
would have to be either copied into Chroma metadata at index time — where a
rename silently makes every existing chunk lie — or resolved with a board request
per recall, to decorate a snippet the model is about to read anyway. The date is
already in the metadata, costs nothing, and is the part that actually helps the
model say "you mentioned this on the 12th".

Chroma metadata gains `session_id`, which is what makes both possible. The
existing boot `sync()` still rebuilds from `assistant.db`, so a turn indexed
before this change simply carries the migrated session.

The system prompt's paragraph beginning "The conversation you see may be a
window" is rewritten: this is one conversation among several, and the others are
searched only when the user refers to something outside it. The old wording
actively encourages the behaviour this spec exists to stop.

`daily_recap` is unaffected — it reports what a day held, which was never scoped
to a conversation.

## Testing

| Layer | File | Covers |
| --- | --- | --- |
| integration | `tests/chat.test.js` | session routes; title derivation; rename refusing empty; soft delete leaving messages; `messageCount`; the migration adopting pre-existing rows into `Earlier conversations` |
| configuration invariant | `tests/context.test.js` | no message is pinned outside the budget any more; the `CHAT_KEEP` / `CONTEXT_*` / `MAX_*` ordering still holds |
| unit | `brain/tests/test_chat_record.py` | `remember()` carries `session_id`, `steps`, `usage`, `cost`; a NULL price stays NULL |
| unit | `brain/tests/test_topics.py` | openers detected; drift decided against a stub embedder with controlled vectors; a failing embedder returns "no drift" |
| eval | `brain/tests/evals/` | the threshold, on labelled pairs against the real embedder |
| unit | `brain/tests/test_tools_retrieve.py` | recall excludes the current session and dates its hits |
| integration | `brain/tests/test_chat_record.py` | `prune` drops chunks for messages the live record no longer returns, so a deleted chat stops answering recall |
| end-to-end | `tests/e2e_test.py` | New chat empties the log; a historic chat reopens with its messages and accepts a new turn; the drift strip renders and both buttons work |

One test per way this can break. The edge cases — an empty title, a session with
no messages, a NULL cost — are extra asserts inside the test they belong to.

**The e2e suite can drive the nudge without an embedder.** It runs
`BRAIN_EMBEDDER=fake`, so signal 2 produces no meaningful distance — but signal 1
is pure pattern matching and needs no model at all. Typing `hi` into a chat that
already has messages fires it deterministically, which is why the opener check
exists as its own signal rather than as a special case inside the distance
calculation. No test-only override is needed, and none is added: a UI path
reachable only under a test flag is a UI path nobody has actually seen work.

## Alternatives considered

### Why not the session boundary alone?

It was the recommendation. Deleting the framing injection and scoping the window
to a session fixes every case in *The bug, precisely* with strictly less code
than exists today, no new route, no embedder on the send path, and nothing to
calibrate. The user's own example — `"hi"` — becomes a one-message request.

It was not chosen because it depends on the user remembering to press the
button. That is a real objection: the failure mode of a manual boundary is
silent, and it lands exactly when you are absorbed in something else, which is
when the assistant answering the wrong question is most expensive. The nudge is
the cost of covering that, and it is deliberately the cheapest form of it —
local patterns first, one embedding second, a suggestion always.

### Why not detect the topic change with an LLM call?

A small model answering "is this a new subject?" would be more accurate than
cosine distance and needs no threshold. It also adds a paid round trip to every
message, doubles the latency before the first token, and makes the send path
depend on the chat provider being up — for a hint. If the eval shows the
embedding signal cannot separate the labelled pairs, this is the next thing to
try, and `DriftVerdict` is the seam it would arrive behind.

### Why not summarise old turns instead of cutting them off?

This project has decided against rolling summaries twice already. A summary is a
lossy copy of the record that costs a call to produce, goes stale silently, and
cannot be audited against the transcript it claims to represent. Sessions make
the question moot: the reason to summarise was that one transcript grew without
bound, and now it does not.

### Why is history in SQLite rather than left in `localStorage`?

`localStorage` was the smallest change and it was rejected by the user, correctly:
a chat history that a cleared browser profile destroys is not a history. It also
splits the truth in two, since `assistant.db` was accumulating the same turns
with nothing reading them.
