"""The two doors onto the board — and neither one writes to it.

`create_card` proposes a card and `update_card` suggests a change to one. Both
stop at a table the owner reads: a proposal is `pending = 1` until he accepts
it, a suggestion sits in `card_edits` until he opens the card, adjusts whatever
he likes and presses Save himself. So the brain still holds no board-write path
at all — `BoardClient` has no whole-board PUT to reach, and that absence is the
guardrail rather than a rule anybody here has to remember.

**The honesty is in the descriptions, because the names cannot carry it.** The
tools are called `create_card` and `update_card` for a mechanical reason: the
board's integration is keyed on those two words. `server.PROPOSING_TOOLS`
decides the `proposed` flag the browser reads to refresh its suggestion lists.
A
better word would cost a server change, a frontend change and a broken
assertion. So each description says *propose* and *suggest*, and says plainly
that the model must not claim the board changed.

**What "use the import function" turned into.** The owner asked for the
dashboard's import path — the model produces the fields, they are injected into
card JSON, and the board validates it. There is no import API to call: import
is entirely in the browser (`js/core/importplan.js`, then its own whole-board
PUT), and handing the brain a whole-board write is forbidden. But
`writeProposal` (`server.js`) already runs the raw JSON it is posted through
the *same* `cleanCard` the import path uses. So that behaviour exists today at
`POST /api/proposals` with no new machinery, and it is why nothing here
validates a card: this module names the fields, and the board is the validator.
The board's own refusal comes back as an exception on purpose — `{'error': …}`
is `ToolErrorMiddleware`'s answer, one place for every tool, and a per-tool
`try` here would be a second one.

**Putting a card in Done is `update_card` with `column_id: 'answered'`.** No
second tool and no `doneAt` argument. `placeCard` stamps the finished moment
when the human's save files the card, so a tool that could send the date could
claim a day nobody finished anything on — which is `pricing.py`'s rule about a
figure nobody measured, applied to a date.

**No delete, no purge, no habit completion.** Not gated — absent. The board's
durability promise is that a card is destroyed only through Trash → *Delete
permanently*, and a habit history a model can write into is not a record.

**Nothing about the card is written out twice.** Both argument models are
`fields.py`'s, both spellings of every field are `fields.py`'s, and a card is
described through `render.py`. Widget 1 declares its field list twice — once
with defaults and once with `| None` — and that is how it came to advertise a
`deadline` the server's allowlist rejected and a `plan` the write path dropped.
Deliberately not carried across from it, beyond the two tool names: its
`planSrc` on an edit (the allowlist drops it, and the *reviewer* is who claims
that provenance now), its `_brief` card shape (a second way to describe a
card), and its "does this id exist" pre-read (the board answers that itself,
and this module reads no board).
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from ...board.client import BoardClient
from ...board.snapshot import board_of
from ...tools.dual import with_sync_door
from .fields import CardEdit, NewCard, to_board
from .render import render_card


def _given(named: dict) -> dict:
    """Only the fields the call actually named.

    `to_board`'s `exclude_unset` is what keeps "clear this deadline" (`''`)
    apart from "never mentioned the deadline", and it cannot survive the
    framework boundary on its own: LangChain hands a tool *every* field of its
    args model, filling the ones the model omitted with their default (checked
    against langchain-core 1.6.2, `BaseTool._parse_input`). Every default here
    is `None` and no field means anything by `None`, so dropping them restores
    exactly the set the model chose — and `''` passes through, which is the
    whole distinction.
    """
    return {key: value for key, value in named.items() if value is not None}


def make_write_tools(board: BoardClient) -> list[BaseTool]:
    """The two writing tools, over one board client.

    A client and not a `BoardSnapshot`: neither tool reads the board. The
    snapshot exists so several *reading* tools in one turn share a fetch, and a
    turn's snapshot could not answer for these two anyway — a proposal and a
    suggestion are both invisible to a board read, being `pending` and a row in
    another table.
    """
    # Which board is the run config's, never the model's: the owner chose one in
    # the picker, and that is not something a tool call may name or mistype.
    @tool('create_card', args_schema=NewCard)
    async def create_card(config: RunnableConfig = None, **named) -> str:
        """Propose a new card for the owner to approve.

        The card is NOT on the board: it waits in his proposals list until he
        accepts it, and it earns its ledger number then. So tell him you have
        proposed it — never that you added it, wrote it down, or saved it.
        Only the words are required; leave a field out and the board's own
        default applies.
        """
        card = to_board(NewCard(**_given(named)))
        # A plan the model chose has to say so, or the board resolves it
        # straight back to the deadline: `planSrc` defaults to 'auto' and
        # `resolvePlan` reads an 'auto' row's plan off its deadline, which is
        # how a proposed plan was measured coming back as '' on 2026-09-08.
        # 'ai' is the truthful provenance for a card no person has looked at
        # yet, and it is not the model's to choose — which is why `planSrc` is
        # absent from the schema and set here, from the presence of a plan.
        if card.get('plan'):
            card['planSrc'] = 'ai'
        proposal = await board.create_proposal(card, board_of(config))
        # Rendered rather than echoed, through the one site that describes a
        # card. `labels` is not passed because this module reads no board: an
        # unresolved category id prints as it stands, which is the only true
        # thing in hand.
        return ('Proposed, and waiting for the owner to accept it — nothing is '
                'on the board yet:\n' + render_card(proposal, level='full'))

    @tool('update_card', args_schema=CardEdit)
    async def update_card(config: RunnableConfig = None, **named) -> str:
        """Suggest a change to a card that is already on the board.

        The change is NOT applied. It goes to the owner as a suggestion he
        opens in the ordinary card editor, adjusts if he wants, and saves
        himself — so say you have suggested a change, never that you made one.

        Name only the fields that should change; anything left out is left
        alone. To finish a card, suggest `column_id: 'answered'` and nothing
        else: 'answered' is the Done column, and the board stamps the finished
        date when he saves.
        """
        edit = CardEdit(**_given(named))
        # `id` is not one of the board's writable fields, so `to_board` leaves
        # it out of the suggestion and it travels as the card being suggested
        # about. What is left is only what this call named — a suggestion
        # carrying every field would overwrite the untouched ones with values
        # that were current when the model looked, which is a stale write
        # wearing an edit's clothes.
        fields = to_board(edit)
        if not fields:
            # The board refuses this too, and the refusal would arrive as an
            # exception. Answered here instead because it is a fact about the
            # call and not about the board, and a sentence is something the
            # model can act on without a round trip.
            return ('Nothing to suggest: name at least one field to change '
                    'besides the id.')
        await board.create_edit(edit.id, fields)
        said = ', '.join(f'{key} → {value!r}' for key, value in fields.items())
        return (f'Suggested for card {edit.id}, and waiting for the owner to '
                f'open it and save — nothing has changed yet: {said}')

    # Both doors, because `Widget2Agent.run` is synchronous and LangChain's sync
    # ToolNode raises on a coroutine-only tool rather than falling back — see
    # `tools/dual.py`, which is a shared service like `board/client.py` and is
    # imported rather than copied.
    return [with_sync_door(t) for t in (create_card, update_card)]


__all__ = ['make_write_tools']
