"""The card, as a set of fields — declared once, used by everything.

Widget 1 writes its card fields out twice: `CreateCardArgs` with defaults and
`UpdateCardArgs` with `| None`. That is not a style complaint. It is how that
agent came to advertise a `deadline` the server's allowlist did not accept and
a `plan` the write path silently drops — two lists that have to agree, kept by
hand, and nothing raises on the day they stop agreeing.

So here the fields are declared once, as `CardFields`, and both doors derive
from it: `NewCard` is `CardFields` with a required title, `CardEdit` is
`CardFields` with a required id. Every other field is optional in both, because
"leave this alone" and "the default" are the same instruction to a validator
that treats an absent field as unset.

**One mapping, both directions.** `to_board` turns this model into the board's
own camelCase JSON, and `BOARD_KEYS` is the whole translation table. Nothing
else in `widget2/` may spell a board field name: a second spelling is the same
hazard one level down.

**What is deliberately not here**, each for its own reason:

- `parentId` — the owner's decision. Filing a card *under another card* is a
  separate question from editing one.
- `habitHistory` — a record of what actually happened. A history a model can
  write into is not a record, which is the same rule that gives this agent no
  habit-completion tool.
- `doneAt` — it follows the column. `placeCard` stamps the finished moment when
  a human's save files the card, so a tool that could set it directly would be
  able to claim a date nobody finished anything on.
- `effortSrc` / `controlSrc` / `planSrc` — provenance is *who chose* a value,
  not what it is. A model that could write these could assert that a person
  made a choice they never made.
- `id` and `num` on a new card — a ledger number belongs to the board to issue,
  and it is earned when a proposal is accepted.

This module imports nothing of the agent's, which is what lets the whole table
be pinned under plain pytest with no board, model or graph. Two tests do it,
and the split is deliberate: `test_widget2_write.py` asserts the two doors are
one field set, and `test_edit_suggestions.py` asserts `EDITABLE_BOARD_KEYS`
equals the server's own allowlist in both directions — the cross-language half,
which is the one the two silent drops of 2026-09-02 came out of.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .spans import plan_period

# ── The board's own vocabularies ────────────────────────────────────────────
# Values, not descriptions: these are `server.js`'s own lists, so a tool cannot
# offer the model a value `cleanCard` would silently replace. Declared here
# because both writers and the reader constrain the same fields, and a second
# copy is a second thing to update when the board grows a card type.
Column = Literal['inbox', 'in-progress', 'answered']
CardType = Literal['question', 'problem', 'task', 'idea', 'dream', 'habit']
Rank = Literal['high', 'low', '']
Effort = Literal['low', 'medium', 'high']
Control = Literal['act', 'influence', 'none']
Cadence = Literal['daily', 'weekly', 'monthly', 'yearly', '']

#: What the columns are called where a person can see them. The id `answered` is
#: **Done** everywhere in the UI, and a model that reads the id will repeat the
#: id back — so the translation belongs beside the vocabulary it translates.
COLUMN_LABELS = {'inbox': 'Inbox', 'in-progress': 'In Progress',
                 'answered': 'Done'}

CATEGORY_HELP = ("the life area's id, lower-case — ask the board which ones "
                 "exist rather than inventing one; '' is uncategorised")
PLAN_HELP = ("when you mean to get to it, at the precision you actually mean: "
             "'2027', '2027-03' or '2027-03-04'. Not a deadline — that is "
             "`deadline`")
TIMES_HELP = ("habits only: one entry per repetition, each a time of day like "
              "'08:00'. Fewer entries than repetitions is fine — the board "
              "pads the rest")


class CardFields(BaseModel):
    """Every field of a card that may be written, all optional.

    Optional in both doors on purpose: on a new card an absent field means "the
    board's default", and on an edit it means "leave this alone". One model can
    serve both because `to_board` sends only what was actually set.
    """

    title: str | None = Field(None, description="the card's own words, one line")
    notes: str | None = Field(None, description='the body text, free-form')
    type: CardType | None = None
    category: str | None = Field(None, description=CATEGORY_HELP)
    column_id: Column | None = Field(
        None, description="where it lives; 'answered' IS Done — this is how a "
                          'card is finished, and the board stamps the date')
    importance: Rank | None = None
    urgency: Rank | None = None
    effort: Effort | None = None
    control: Control | None = Field(
        None, description="how much of it is yours to do: 'act' (mine), "
                          "'influence', 'none'")
    deadline: str | None = Field(
        None, description="when it is due: 'YYYY-MM-DD', or '' for none")
    plan: str | None = Field(None, description=PLAN_HELP)
    tags: list[str] | None = Field(None, description='replaces the whole list')
    habit_freq: Cadence | None = Field(
        None, description='habits only: how often the period comes round')
    habit_count: int | None = Field(
        None, ge=1, le=99,
        description='habits only: repetitions per period (2 with "daily" '
                    'means twice a day)')
    habit_times: list[str] | None = Field(None, description=TIMES_HELP)


    @model_validator(mode='after')
    def _plan_opens_before_the_deadline(self) -> 'CardFields':
        """The one pair of fields the board refuses, checked in one place.

        A plan whose window opens after the card was already due cannot be
        saved: the card dialog refuses it, so a suggestion naming that pair is
        a suggestion the owner cannot apply. Widget 1 checks it too
        (`_plan_error`) and returns a message; here it is a validator on the
        shared model, so **both doors get it and neither can forget it** — and
        a proposal is covered, which is the case that actually escapes. A
        proposal is confirmed by `POST /api/proposals/:id/confirm` and never
        passes through the dialog, so without this a proposed card could carry
        a conflict a hand-typed one is refused for, and accepting it would put
        that conflict on the board.

        **Only when the call names both.** An edit that sets a plan alone
        cannot be judged here — the stored deadline is on the board and this
        model is not allowed to read it, which is the same reason
        `exclude_unset` matters one function down. The dialog is the backstop
        for that case, and it is a real one.

        Raised rather than returned: LangChain turns an argument-validation
        error into a readable tool message on its own, so the model is told
        what is wrong and can fix the plan — or say the deadline should move —
        instead of filing something unusable.
        """
        given = self.model_dump(exclude_unset=True)
        plan, deadline = given.get('plan'), given.get('deadline')
        if not plan or not deadline:
            return self
        period = plan_period(plan)
        if period is None:
            return self
        try:
            due = date.fromisoformat(deadline)
        except ValueError:
            return self          # the board's own validator will refuse it
        if period[0] > due:
            raise ValueError(
                f'plan {plan} starts after the deadline {deadline} — plan it '
                f'earlier, or suggest moving the deadline first')
        return self


class NewCard(CardFields):
    """A card to propose. Only the words are required — everything else has a
    board default, and a coach who had to fill in nine fields to write down a
    thought would be a worse coach."""

    title: str = Field(description="the card's own words, one line")


class CardEdit(CardFields):
    """A change to suggest. The id says which card; every field left unset is
    left alone, so a one-field suggestion is a one-field suggestion."""

    id: str = Field(description='the id of the card to change, as the board '
                                'gave it — never a ledger number like C-042')


#: The whole snake_case → camelCase table, and the only place either spelling of
#: a field appears next to the other. Keys are `CardFields`' own names, so a
#: field added to the model and not to this table is caught by the test that
#: asserts the two are exhaustive in both directions.
BOARD_KEYS = {
    'title': 'title',
    'notes': 'notes',
    'type': 'type',
    'category': 'category',
    'column_id': 'columnId',
    'importance': 'importance',
    'urgency': 'urgency',
    'effort': 'effort',
    'control': 'control',
    'deadline': 'deadline',
    'plan': 'plan',
    'tags': 'tags',
    'habit_freq': 'habitFreq',
    'habit_count': 'habitCount',
    'habit_times': 'habitTimes',
}

#: What `server.js`'s `EDITABLE_FIELDS` must hold, in the board's own spelling.
#: Exported so one test can assert the server's allowlist and this schema are
#: the same set rather than two lists somebody keeps in step by hand — the
#: failure this module exists to end.
EDITABLE_BOARD_KEYS = frozenset(BOARD_KEYS.values())


def to_board(fields: CardFields) -> dict:
    """The fields that were actually set, in the board's own spelling.

    `exclude_unset` and not `exclude_none`: a field explicitly set to `''` is an
    instruction — clearing a deadline, un-filing a category — and one the model
    never mentioned is not. Telling those apart is the whole reason both doors
    take an all-optional model.
    """
    given = fields.model_dump(exclude_unset=True)
    return {BOARD_KEYS[key]: value for key, value in given.items()
            if key in BOARD_KEYS}


__all__ = ['BOARD_KEYS', 'COLUMN_LABELS', 'EDITABLE_BOARD_KEYS', 'Cadence',
           'CardEdit', 'CardFields', 'CardType', 'Column', 'Control', 'Effort',
           'NewCard', 'Rank', 'to_board']
