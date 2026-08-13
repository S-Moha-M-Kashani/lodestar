"""The board API, one board at a time — and no way to write a card.

`create_card` proposes one and `update_card` suggests a change to one; both wait
for the user, who applies them by saving the board themselves. So this client
carries no whole-board PUT at all, which is what retires the old rule about never
sending a partial card list: there is no list to send. The agent reads the board,
and asks.

Every call is a coroutine. The brain answers on an async route and a turn is
mostly waiting — for the model, then for the board — so a blocking `httpx.get`
here pins the event loop for the length of a board read while nothing else can
be served. The request is made through a client of its own rather than a pooled
one: a pool binds to the loop that created it, and this process has more than one
(the test client opens a fresh loop per request), so a shared pool is a closed
loop waiting to happen. The sync client it replaces opened a connection per call
too, so nothing was pooled before either.
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

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            res = await http.get(f'{self.base_url}{path}', params=params or {})
        res.raise_for_status()
        return res.json()

    async def _post(self, path: str, json: dict,
                    params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            res = await http.post(f'{self.base_url}{path}', json=json,
                                  params=params or {})
        res.raise_for_status()
        return res.json()

    async def list_cards(self, board_id: str = '') -> list[dict]:
        body = await self._get('/api/state', self._scope(board_id))
        return body['cards']

    async def list_chat(self, board_id: str = '') -> list[dict]:
        """The live chat record for one board, oldest first."""
        body = await self._get('/api/chat/messages', self._scope(board_id))
        return body['messages']

    async def list_all_chat(self) -> list[dict]:
        """Every board's live messages, for maintaining the chat index.

        The index is one collection over the whole record and `prune` deletes
        chunks whose message is no longer live — so syncing it from a single
        board's messages would drop every other board out of recall. The only
        caller is index maintenance; everything a person reads is scoped.
        """
        body = await self._get('/api/chat/messages/all')
        return body['messages']

    async def record_chat(self, messages: list[dict],
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
        body = await self._post('/api/chat/messages', payload)
        return body['messages']

    async def create_proposal(self, card: dict, board_id: str = '') -> dict:
        """Offer one card for the user's approval.

        On its own endpoint, never the whole-board PUT — which this client no
        longer has at all. Removing it is the guardrail: the brain cannot write a
        card even by mistake, so "never send a partial card list" stops being a
        rule anyone has to remember here.
        """
        return await self._post('/api/proposals', card, self._scope(board_id))

    async def create_edit(self, card_id: str, fields: dict) -> dict:
        """Offer a change to an existing card, for the user to review and save.

        The counterpart to create_proposal, and for the same reason: this cannot
        reach the whole-board PUT, so an agent edit is a note about a card rather
        than a write to one. Nothing on the board moves until the user saves.
        """
        return await self._post('/api/edits',
                                {'cardId': card_id, 'fields': fields})


__all__ = ['BoardClient']
