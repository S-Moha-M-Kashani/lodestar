"""The board API, one board at a time — and no way to write a card.

`create_card` proposes one and `update_card` suggests a change to one; both wait
for the user, who applies them by saving the board themselves. So this client
carries no whole-board PUT at all, which is what retires the old rule about never
sending a partial card list: there is no list to send. The agent reads the board,
and asks.
"""
import httpx


class BoardClient:
    """The board API, one board at a time.

    Every call takes an optional `board_id`, and an empty one is *omitted*
    rather than sent blank — the server has to be able to tell "no board named"
    (answer with the default board, which is what every caller written before
    boards existed relies on) from "a board named the empty string". The id
    itself never comes from the model: it rides the agent's run config.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    @staticmethod
    def _scope(board_id: str = '') -> dict:
        return {'board': board_id} if board_id else {}

    def list_cards(self, board_id: str = '') -> list[dict]:
        res = httpx.get(f'{self.base_url}/api/state',
                        params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()['cards']

    def list_chat(self, board_id: str = '') -> list[dict]:
        """The live chat record for one board, oldest first."""
        res = httpx.get(f'{self.base_url}/api/chat/messages',
                        params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def list_all_chat(self) -> list[dict]:
        """Every board's live messages, for maintaining the chat index.

        The index is one collection over the whole record and `prune` deletes
        chunks whose message is no longer live — so syncing it from a single
        board's messages would drop every other board out of recall. The only
        caller is index maintenance; everything a person reads is scoped.
        """
        res = httpx.get(f'{self.base_url}/api/chat/messages/all',
                        timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def record_chat(self, messages: list[dict],
                    session_id: str = '', board_id: str = '') -> list[dict]:
        """Append to the durable chat record (assistant.db) — through the Node
        API like every write, never SQLite directly. Returns the inserted rows
        with their ids, which is what the Chroma index chunks are keyed on.

        An empty `session_id` is omitted rather than sent as '': the server files
        an unnamed batch under its reserved 'adhoc' chat, and it can only do that
        if it can tell "no session named" from "a session named the empty
        string"."""
        payload: dict = {'messages': messages}
        if session_id:
            payload['sessionId'] = session_id
        # In the body, not the query string: this is the one chat route the
        # brain posts to, and the board travels beside the session it belongs
        # with rather than in a different part of the request.
        if board_id:
            payload['boardId'] = board_id
        res = httpx.post(f'{self.base_url}/api/chat/messages',
                         json=payload, timeout=self.timeout)
        res.raise_for_status()
        return res.json()['messages']

    def create_proposal(self, card: dict, board_id: str = '') -> dict:
        """Offer one card for the user's approval.

        On its own endpoint, never the whole-board PUT — which this client no
        longer has at all. Removing it is the guardrail: the brain cannot write a
        card even by mistake, so "never send a partial card list" stops being a
        rule anyone has to remember here.
        """
        res = httpx.post(f'{self.base_url}/api/proposals', json=card,
                         params=self._scope(board_id), timeout=self.timeout)
        res.raise_for_status()
        return res.json()

    def create_edit(self, card_id: str, fields: dict) -> dict:
        """Offer a change to an existing card, for the user to review and save.

        The counterpart to create_proposal, and for the same reason: this cannot
        reach the whole-board PUT, so an agent edit is a note about a card rather
        than a write to one. Nothing on the board moves until the user saves.
        """
        res = httpx.post(f'{self.base_url}/api/edits',
                         json={'cardId': card_id, 'fields': fields},
                         timeout=self.timeout)
        res.raise_for_status()
        return res.json()


__all__ = ['BoardClient']
