"""How text is cut, and what shape a card takes on the way into a store.

**The splitter.** `RecursiveCharacterTextSplitter`, with the Persian sentence
enders in its separator list, so a chat transcript is cut at a boundary a reader
would recognise rather than at character 500.

**The document shape.** A board card becomes one `Document` whose metadata is
flat, complete and filterable. Complete matters: a key only some documents carry
turns a `where` clause into a silent partial scan, which reads as a retrieval
bug rather than the schema bug it is.

Both sides implement LangChain's own interfaces (`Document`), so the retrievers
assembled on top of them accept these objects without adapters.
"""
import json
from datetime import datetime, timezone

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 500 characters keeps the granularity chat memory has always had; the overlap
# is the reason for the recursive splitter at all — a thought cut in half is
# whole in one of the two windows.
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100

# Cut at the largest boundary that fits. The Persian enders are in the list
# because the text is Farsi typed by a human: without «؟» and «،» a Persian
# paragraph falls through to the space separator and is cut mid-clause.
SEPARATORS = ['\n\n', '\n', '. ', '؟ ', '? ', '! ', '؛ ', '، ', ' ', '']

_SPLITTER = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
    separators=SEPARATORS, keep_separator=True)


def split_text(text: str) -> list[str]:
    """Chunk a transcript. Blank pieces are dropped rather than indexed: an
    empty document is a row that matches nothing and dilutes every score."""
    return [chunk.strip() for chunk in _SPLITTER.split_text(text)
            if chunk.strip()]


# --- documents --------------------------------------------------------------

# Every key, on every document. A field only some rows carry turns a `where`
# clause into a silent partial scan.
CARD_META_KEYS = ('id', 'num', 'title', 'columnId', 'type', 'category', 'tags',
                  'created_day', 'updated_day')


def day_int(epoch_ms) -> int:
    """1773135000000 -> 20260310. Metadata filters compare numbers, not date
    strings, so the date rides as an int and a time scope is a $gte/$lte pair.
    UTC, deliberately: a filter that moves with the reader's timezone would make
    the same query match different cards on different machines. 0 for a missing
    timestamp — outside every real range, so it is excluded rather than
    matching everything."""
    if not isinstance(epoch_ms, (int, float)) or epoch_ms <= 0:
        return 0
    at = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return at.year * 10000 + at.month * 100 + at.day


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float, bool))


def flatten_metadata(metadata: dict) -> dict:
    """Make a metadata dict a store will accept and a filter can read.

    Chroma takes scalars only. A list of scalars is space-joined rather than
    JSON-encoded, because a joined string is still searchable and filterable
    while a JSON string is neither — you cannot query inside one. `None` is
    dropped: the store rejects null, and an absent key fails a reader loudly
    instead of grouping every row under a nonexistent value. Anything genuinely
    nested survives under '<key>_json' so the body is not lost, with the
    understood cost that it cannot be filtered on."""
    flat: dict = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if _is_scalar(value):
            flat[key] = value
        elif isinstance(value, (list, tuple)) and all(map(_is_scalar, value)):
            flat[key] = ' '.join(str(item) for item in value)
        else:
            flat[f'{key}_json'] = json.dumps(value)
    return flat


def card_text(card: dict) -> str:
    """What a card looks like to the retriever: title, notes and tags — the
    words the user actually wrote — plus the type and category labels they
    filed it under, so "habit" or "love" finds the cards stamped that way."""
    parts = [card.get('title') or '', card.get('notes') or '',
             ' '.join(card.get('tags') or []),
             card.get('type') or '', card.get('category') or '']
    return ' '.join(part for part in parts if part).strip()


def card_document(card: dict) -> Document:
    """One card as one document. The title is repeated in the metadata even
    though it is in the text: a caller building a result list needs the title
    alone, and re-reading the board to get it would be a second round trip
    behind every tool call."""
    metadata = flatten_metadata({
        'id': card.get('id') or '',
        'num': card.get('num') or 0,
        'title': card.get('title') or '',
        'columnId': card.get('columnId') or '',
        'type': card.get('type') or '',
        'category': card.get('category') or '',
        'tags': card.get('tags') or [],
        'created_day': day_int(card.get('createdAt')),
        'updated_day': day_int(card.get('updatedAt')),
    })
    # flatten_metadata drops nothing here — every value above is a scalar or a
    # list — but an empty tag list joins to '', so the key set stays complete.
    metadata.setdefault('tags', '')
    return Document(id=card.get('id') or '', page_content=card_text(card),
                    metadata=metadata)


"""Alternatives considered

**"Why is `flatten_metadata` yours? `langchain-chroma` has a filter for this."**

*Short answer.* Because the framework's version deletes data and this one
converts it. Both make Chroma accept the record; only one leaves the tags
searchable.

*Why the obvious option fails.* `filter_complex_metadata` drops any value that
is not a scalar. A card's `tags` is a list, so the framework's helper silently
removes the field — and the tags are among the few words on a card that the user
chose as an index term. Nothing raises; the card is simply harder to find
afterwards, which surfaces months later as "search does not work" rather than as
an error.

*Why not the framework, and the libraries.* There is no third option here worth
naming: Chroma's own client raises on a non-scalar, `filter_complex_metadata`
drops it, and a JSON blob under the original key would be accepted but
unfilterable — you cannot query inside a JSON string, so it would look like a
working filter that never matches. Space-joining is the only one of the three
that keeps the value both stored and queryable. (Preserving lists properly would
mean a store with typed array fields — Qdrant, Weaviate, Postgres with
`pgvector` — a much larger decision than this function.) The splitter itself is
taken from the framework unchanged; only its separator list is ours.

*Why not adopted, and what would change it.* Joining is lossy in one specific
way: a tag containing a space becomes two tokens. That is acceptable while tags
are single words, and the board's tag input does not encourage otherwise. If
tags become phrases, the fix is not this function — it is moving the chat store
off Chroma to something with a real array type, and that is a Session-5-sized
argument, not a helper.
"""
