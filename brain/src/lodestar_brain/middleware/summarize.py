"""What a long conversation costs, bounded twice — cheaply first.

Nothing in this brain used to shrink a context. The browser re-sends the whole
transcript every turn, the thread adds every tool result on top of it, and at 80
messages or 120 000 characters the route simply refused (`server.py`). A refusal
is an honest answer to "this will not fit", but it is not an answer to "this
costs too much", which is the complaint.

Two middlewares, both LangChain's, assembled here because the *order and the
thresholds* are the design and neither belongs in the graph module:

1. **Clearing tool output** (`ContextEditingMiddleware` + `ClearToolUsesEdit`).
   Past `clear_tools_tokens`, every tool result but the newest
   `clear_tools_keep` is replaced by a placeholder. This is the cheap defence:
   it costs no model call, it touches nothing the *user* said, and what it drops
   is the one part of the transcript the model has already read and acted on.
   It also edits a copy for the request rather than the thread's state, so a
   later turn is free to make a different decision about the same messages.
2. **Summarising** (`SummarizationMiddleware`). Past `summary_tokens` the older
   turns are collapsed into one summary and the newest `summary_keep` messages
   are kept verbatim. This one spends a model call and is lossy, so its trigger
   sits at twice the first one's. That was meant to make it the second rung of a
   ladder — dropping tool output tried first and found wanting — and measurement
   says it is not one: the trigger counts a thread the clearing never touched.
   See "Why 8 000 and 4 000" below.

**Why 8 000 and 4 000.** The route refuses past 120 000 characters, which is
roughly 30 000 approximate tokens, so 8 000 leaves a summariser several turns of
room before the cap it exists to make unnecessary — and an ordinary turn on this
board is a few hundred tokens, so nothing routine crosses it. They are
deliberately conservative rather than optimal: summarisation is the one change
here that can alter what the model *says*, and a threshold that is too high costs
money, while one that is too low costs answers.

Two things about them are now measured (2026-08-13), and neither is the value.

**Neither trigger has ever been reached.** Across the live `assistant.db` and all
100 backups of it, the largest conversation this board has ever held is 20
messages and ~2 060 approximate tokens — half the cheap trigger and a quarter of
the summariser's. Both defences are, against every conversation on record, dead
code; the numbers describe a conversation nobody has had yet, which is why the
experiment below cannot be run today and is written down instead:

    sqlite3 databases/real/assistant.db "select session_id, count(*),
      sum(length(content)) from messages where deleted_at is null
      group by session_id order by 3 desc"

**The 1:2 ratio does not do what it says.** `tests/evals/test_context_budget.py`
replays a tool-heavy conversation through this stack. On it, clearing alone takes
the peak request from 9 720 to 3 654 approximate tokens and never needs help;
adding the summariser at 8 000 spends a model call and a lossy rewrite to take a
further **zero** off that peak, because the two are hooked into different places.
`SummarizationMiddleware` is a `before_model` state hook and counts the *thread*,
which holds every tool result in full for ever; `ContextEditingMiddleware` wraps
the model call and edits a *copy*. So the cheap defence's saving is real on the
wire and invisible to the expensive defence's trigger, and "by the time it fires,
dropping tool output has already been tried and was not enough" is not something
the trigger is in a position to know.

What that measurement changes is the reasoning, not the constants. The two are
not one defence at two severities but answers to two different causes of growth:
clearing bounds the part of the context that is *tool output*, summarisation the
part that is *spoken*, and only the second grows without a ceiling. 4 000 and
8 000 are kept — nothing measured says where else to put them — but the ladder
they were arranged into is not there, and a future change should size them
against the two quantities separately rather than at a ratio to each other.

**The experiment that would settle the values**, in the detail it needs to be
runnable the day the input exists:

- *Input*: at least twenty real conversations, one subject each, each past 8 000
  approximate tokens of **spoken** text with tool output excluded, in both
  languages this board is used in. Segmented per chat — the `sessions` table
  gives that now, the pre-sessions rows do not.
- *Label*: per conversation, one probe question whose correct answer is a fact
  stated in the span summarisation would collapse (anything older than the newest
  `summary_keep` messages), plus the exact string the reply has to contain.
- *Vary*: `summary_tokens` over 4 000 / 8 000 / 16 000 / 32 000, against the model
  that answers, holding `summary_keep` fixed.
- *Score*: substring match of the labelled fact in the reply — the injection
  eval's discipline, not a judge — plus the dollars `pricing.py` reports per turn.
- *Read it*: recall flat across the triggers means 8 000 is too low and should
  rise until cost rather than recall binds. Recall falling as the trigger rises
  means the collapsed span is the problem, and the fix is `summary_keep` or
  llmlingua-style compression rather than a bigger number. Recall already poor at
  8 000 means summarising is the wrong defence here and the 120 000-character
  refusal should be left to do its job.
- *Cost*: one summariser call plus one answer per conversation per trigger — some
  eighty turns, single-digit dollars. **The corpus is what is missing, not the
  budget.**

**The summariser is the model that answers.** No second seam: `make_chat_model`
already builds it, and a summariser on a different model would mean a
conversation compressed by something the user never chose. The cost of that call
is invisible in the turn's `usage`, because it is a direct model invocation and
not a message in the graph — a known gap, not an accounting bug.
"""
from __future__ import annotations

from langchain.agents.middleware import (ClearToolUsesEdit,
                                         ContextEditingMiddleware,
                                         SummarizationMiddleware)
from langchain_core.language_models import BaseChatModel

from ..config import Settings


def make_context_editor(settings: Settings) -> ContextEditingMiddleware | None:
    """Drop old tool output once the context passes `clear_tools_tokens`.

    None when the trigger is 0, so switching it off means no middleware at all
    rather than one that is asked every turn and always says no.

    `clear_tool_inputs` stays False — the *call* remains visible even when its
    answer is a placeholder. Without that the model reads a transcript in which
    it apparently did nothing, and asks again, which is the loop this is meant to
    make cheaper.
    """
    if settings.clear_tools_tokens <= 0:
        return None
    return ContextEditingMiddleware(edits=[ClearToolUsesEdit(
        trigger=settings.clear_tools_tokens,
        keep=settings.clear_tools_keep,
        clear_tool_inputs=False)])


def make_summarizer(settings: Settings,
                    model: BaseChatModel) -> SummarizationMiddleware | None:
    """Collapse the older turns once the thread passes `summary_tokens`.

    The trigger is an absolute token count rather than a fraction of the model's
    window: the picker moves between a local model and a hosted one mid
    conversation, and a fraction would summarise at a different point depending
    on which one happened to answer — the same conversation, compressed or not
    by an accident of routing. `keep` is a message count for the same reason.
    """
    if settings.summary_tokens <= 0:
        return None
    return SummarizationMiddleware(model,
                                   trigger=('tokens', settings.summary_tokens),
                                   keep=('messages', settings.summary_keep))


__all__ = ['make_context_editor', 'make_summarizer']

"""Alternatives considered
========================

Why did you write your own context budget?
------------------------------------------

Mostly I did not: both middlewares are LangChain 1.3.14's own, and this module is
two factory functions and four numbers. What is ours is the *policy* — which
defence fires first, at what threshold, and on which model — and that is the part
no library can supply, because it is a statement about this board's
conversations and this user's bill.

**Why the obvious option fails.** The obvious option is the one that was here:
refuse. `_refuse_if_oversized` returns 413 past 80 messages or 120 000
characters, and it is still there, because a hard cap is the right answer to a
request that cannot be served. It is the wrong answer to a request that can be
served for less. Concretely: a forty-turn conversation about a house move costs
its full transcript on every one of those forty turns, and the user's remedy is
to start a new chat and lose the thread — the project's own "never lose a
thought" pillar, defeated by an accounting problem.

The second obvious option is a sliding window — keep the last N messages, drop
the rest. It fails on this board in particular: the first message of a chat is
usually the *question*, and a window drops the question and keeps the
follow-ups. Summarisation keeps the intent and drops the wording, which is the
right way round.

**Why not the framework.** It *is* the framework. `SummarizationMiddleware`
handles the two things a hand-rolled version gets wrong — it never splits an
AIMessage from the ToolMessages answering it, and it trims what it feeds the
summariser so that compressing a huge context does not itself need a huge
context. `ClearToolUsesEdit` implements Anthropic's `clear_tool_uses_20250919`
semantics, placeholder and all. Reimplementing either would be re-deriving
tested code to end up with the same behaviour and our own bugs.

One framework trap is worth recording, because it cost an afternoon:
`create_agent(cache=...)` looks like the answer to repeated tool calls and is
inert. It forwards to `.compile(cache=)`, but `create_agent` sets `cache_policy`
on no node and LangGraph has no tool-level caching, so the parameter is accepted,
does nothing, and reports nothing. That is why the tool cache next door is
middleware (`cache.py`) rather than a constructor argument.

**The libraries that would do it** (checked 2026-08-13):

- **LangMem** — LangChain's own memory package: summarisation plus extracted
  profile/semantic memories over a `BaseStore`. The closest thing to a drop-in
  for both this module and `memory.py`.
- **mem0** — hosted or self-hosted memory layer with its own extraction and
  vector store. Strong at "what does this user always want", weak at "make this
  one request smaller".
- **Zep** — a service that keeps the session and returns a compressed context
  window per turn. Genuinely good at exactly this problem, and a container plus
  an account.
- **llmlingua** (Microsoft) — prompt *compression* rather than summarisation:
  drops low-information tokens with a small model, ~2–5× on long prompts, no
  semantic rewrite. The interesting one, and it is orthogonal — it would sit
  under this, not instead of it.
- Greenfield, on someone else's budget: LangMem for the long-term half and this
  middleware pair for the per-turn half, because they answer different questions.

**Why they were not adopted, and what would change it.** Decisively: the
middlewares are already installed and already tested, and every alternative is a
new dependency for a board that runs on one machine. LangMem is the real
candidate and was declined on scope rather than on merit — it wants to own fact
extraction too, and this design deliberately keeps a memory write as a *visible
tool call* (`memory.py`), which an automatic extractor is the opposite of.
What would change it: a measured answer-quality loss from summarisation. If
replaying real conversations at these thresholds shows the summary dropping
things the user then had to repeat, the fix is not a bigger number — it is
llmlingua-style compression that never rewrites, or Zep's per-turn window, and
the trigger becomes the knob that chooses between them. That replay is specified
turn by turn in the module docstring above and is **blocked on its input**: no
conversation on this board, live or in a hundred backups, has ever reached even
half of `clear_tools_tokens`, so there is nothing yet to replay. The half that
could be measured without a corpus has been (`test_context_budget.py`), and it
already points llmlingua's way for one of the two growth causes: on a tool-heavy
conversation the summariser rewrites turns to save nothing the placeholder had
not saved already.
"""
