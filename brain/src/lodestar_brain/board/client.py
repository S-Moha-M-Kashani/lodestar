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

from ..net import aretry


class BoardClient:
    """The board API, one board at a time.

    Every call takes an optional `board_id`, and an empty one is *omitted*
    rather than sent blank — the server has to be able to tell "no board named"
    (answer with the default board, which is what every caller written before
    boards existed relies on) from "a board named the empty string". The id
    itself never comes from the model: it rides the agent's run config.
    """

    def __init__(self, base_url: str, timeout: float = 10.0, token: str = ''):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        # The board requires a login now, even over loopback, and the brain is
        # not a person: it presents a shared service token rather than the
        # user's password, so the plaintext lives in one head and in no
        # service's environment. Empty means no header at all — and therefore
        # 401 on every call, which is the right direction to fail in. A board
        # that answers an unconfigured brain is a board that answers anything.
        self._token = token.strip()

    @staticmethod
    def _scope(board_id: str = '') -> dict:
        return {'board': board_id} if board_id else {}

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._token}'} if self._token else {}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        # Reads are idempotent, so a transient failure gets one retry
        # (net.aretry). raise_for_status sits inside the attempt: a 500 is a
        # failure of the attempt, not of the call.
        async def attempt() -> dict:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                res = await http.get(f'{self.base_url}{path}',
                                     params=params or {},
                                     headers=self._headers())
            res.raise_for_status()
            return res.json()
        return await aretry(attempt)

    async def _post(self, path: str, json: dict,
                    params: dict | None = None) -> dict:
        # Never retried: every POST here writes (a chat row, a proposal, an
        # edit), and a retried write is a duplicate.
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            res = await http.post(f'{self.base_url}{path}', json=json,
                                  params=params or {},
                                  headers=self._headers())
        res.raise_for_status()
        return res.json()

    async def list_board(self, board_id: str = '') -> dict:
        """One board's whole document: its cards AND its category registry.

        The registry is why this exists beside `list_cards`. The map embeds a
        card as its words plus the *labels* it is filed under — "Music", not
        "music" — so the text a vector is made from needs the registry that
        arrives in the same response. Reading it through a second call would be
        a second round trip for data the first one already carried.
        """
        return await self._get('/api/state', self._scope(board_id))

    async def list_cards(self, board_id: str = '') -> list[dict]:
        return (await self.list_board(board_id))['cards']

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

    async def record_trace(self, trace: dict) -> dict:
        """File one turn's developer trace with the board.

        Written twice per turn — in flight, then settled — and the board upserts
        on `trace_id`, so one turn is one record however many times it is filed.
        A separate table from the chat record and deliberately so: the record is
        a conversation, this is the request that produced it, and a debugging
        aid must not be able to change what the transcript says.
        """
        return await self._post('/api/trace', trace)

    async def create_proposal(self, card: dict, board_id: str = '') -> dict:
        """Offer one card for the user's approval.

        On its own endpoint, never the whole-board PUT — which this client no
        longer has at all. Removing it is the guardrail: the brain cannot write a
        card even by mistake, so "never send a partial card list" stops being a
        rule anyone has to remember here.
        """
        return await self._post('/api/proposals', card, self._scope(board_id))

    async def _put(self, path: str, json: dict,
                   params: dict | None = None) -> dict:
        # Not retried, like `_post`: a retried write is a duplicate. This one
        # happens to be idempotent — the board upserts on (card, model) — but
        # the rule is about the method, not about who is on the other end of it.
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            res = await http.put(f'{self.base_url}{path}', json=json,
                                 params=params or {},
                                 headers=self._headers())
        res.raise_for_status()
        return res.json()

    async def list_vectors(self, model: str, board_id: str = '') -> list[dict]:
        """One board's stored card embeddings, for one model.

        The model is required and never defaulted: an answer mixing two
        embedders' geometries is worse than no answer, so a request that names
        none is refused by the board rather than answered with everything.
        """
        body = await self._get('/api/vectors',
                               {**self._scope(board_id), 'model': model})
        return body['vectors']

    async def write_vectors(self, model: str, vectors: list[dict],
                            board_id: str = '') -> dict:
        """Store card embeddings the brain has just computed.

        This is a write, and it is on this client for the reason every brain
        write is: the brain never touches SQLite (invariant 2), so the board's
        own API is the only way in. It is emphatically **not** a card write —
        `test_the_brain_has_no_way_to_write_a_card` still holds, and the reason
        is not a convention here but the shape of the surface: there is no
        whole-board PUT to reach, this route writes a table that is not `cards`,
        and the board refuses a vector for a card it does not hold. A vector is
        derived from a card; losing every one of them costs time and no data.
        """
        return await self._put('/api/vectors', {'model': model, 'vectors': vectors},
                               self._scope(board_id))

    async def create_edit(self, card_id: str, fields: dict) -> dict:
        """Offer a change to an existing card, for the user to review and save.

        The counterpart to create_proposal, and for the same reason: this cannot
        reach the whole-board PUT, so an agent edit is a note about a card rather
        than a write to one. Nothing on the board moves until the user saves.
        """
        return await self._post('/api/edits',
                                {'cardId': card_id, 'fields': fields})


__all__ = ['BoardClient']
