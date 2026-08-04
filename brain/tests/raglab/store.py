"""The lab's vector store: process memory, and nothing else.

An experiment's *material* is not a record. Its chunks, vectors, retrieved
contexts and generated answers exist to produce one number and then stop being
interesting, so they live for the process and are discarded with it. What is
written down is the account of the run — the JSON file `evaluate.save_run`
writes, and the row `ledger.py` records in `raglab.db` — never the vectors it
ran over.

That is why this module exists rather than a Chroma client. A database would
survive a restart, which sounds like a saving and is actually a liability: a
sweep drops and rebuilds an index dozens of times, an interrupted build leaves
rows the current config never produced, and the cheapest way for a stale
collection to be found is to still be there. Brute-force cosine over a few
thousand chunks is also simply faster than HNSW behind an HTTP round-trip at
this corpus size.

**The return shapes are Chroma's on purpose** — `{'ids': [[…]], 'distances':
[[…]]}`, one row per query vector, cosine *distance* rather than similarity.
`LabIndex.dense` turns a distance into a score with `1 - d`, so keeping the
contract identical means swapping the store cannot move a measured number. The
awkward nesting is the price of that, and it is worth paying once.

Filters use Chroma's operator dicts, because `query.where_clause` builds them
and its overlap semantics are load-bearing. Two rules are copied deliberately:
an absent metadata key never satisfies a filter (which is why
`Chunk.metadata()` carries every field on every chunk), and an operator this
module does not implement *raises* instead of being ignored — a filter that
silently does nothing turns a retrieval bug into a scoring one.
"""
import numpy as np


def _compare(value, wanted, test) -> bool:
    """Order comparisons on metadata that may hold anything. A string date
    against an int scope is a filter that cannot hold, not a crash."""
    try:
        return bool(test(value, wanted))
    except TypeError:
        return False


OPERATORS = {
    '$eq': lambda value, wanted: value == wanted,
    '$ne': lambda value, wanted: value != wanted,
    '$gt': lambda value, wanted: _compare(value, wanted, lambda a, b: a > b),
    '$gte': lambda value, wanted: _compare(value, wanted, lambda a, b: a >= b),
    '$lt': lambda value, wanted: _compare(value, wanted, lambda a, b: a < b),
    '$lte': lambda value, wanted: _compare(value, wanted, lambda a, b: a <= b),
    '$in': lambda value, wanted: value in wanted,
    '$nin': lambda value, wanted: value not in wanted,
}


def matches(metadata: dict, where: dict | None) -> bool:
    """Whether one record satisfies a Chroma-style filter. No clause means
    every record, which is how an unscoped question searches everything."""
    if not where:
        return True
    for key, condition in where.items():
        if key == '$and':
            if not all(matches(metadata, clause) for clause in condition):
                return False
        elif key == '$or':
            if not any(matches(metadata, clause) for clause in condition):
                return False
        elif not _holds(metadata, key, condition):
            return False
    return True


def _holds(metadata: dict, key: str, condition) -> bool:
    if key not in metadata:
        return False
    value = metadata[key]
    if not isinstance(condition, dict):
        return value == condition
    for operator, wanted in condition.items():
        test = OPERATORS.get(operator)
        if test is None:
            raise ValueError(
                f'unsupported filter operator {operator!r}; the lab store '
                f'implements ' + ', '.join(sorted(OPERATORS)))
        if not test(value, wanted):
            return False
    return True


def _unit(vectors: np.ndarray) -> np.ndarray:
    """Row-normalised, because cosine is only cosine on unit vectors. A
    zero row stays zero rather than becoming NaN: `ascii-hash` embeds Farsi to
    the zero vector, and that has to score badly, not crash the run."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1.0, norms)


class MemoryVectors:
    """One named, in-process index. `name` is only a label — it comes from
    `IndexConfig.collection()` so a run and the panel can say which
    configuration produced the numbers."""

    def __init__(self, name: str = ''):
        self.name = name
        self.ids: list[str] = []
        self.documents: list[str] = []
        self.metadatas: list[dict] = []
        self._rows: list[np.ndarray] = []
        self._at: dict[str, int] = {}
        self._matrix: np.ndarray | None = None   # built lazily, per query batch

    def count(self) -> int:
        return len(self.ids)

    def upsert(self, ids: list[str], documents: list[str], embeddings,
               metadatas: list[dict] | None = None) -> None:
        """Add or replace by id. Chunk ids are deterministic, so a rebuild
        writes the same ids again and must overwrite rather than duplicate."""
        metadatas = metadatas if metadatas is not None else [{}] * len(ids)
        for chunk_id, document, vector, metadata in zip(ids, documents,
                                                        embeddings, metadatas):
            row = np.asarray(vector, dtype=np.float32).reshape(-1)
            at = self._at.get(chunk_id)
            if at is None:
                self._at[chunk_id] = len(self.ids)
                self.ids.append(chunk_id)
                self.documents.append(document)
                self.metadatas.append(dict(metadata or {}))
                self._rows.append(row)
            else:
                self.documents[at] = document
                self.metadatas[at] = dict(metadata or {})
                self._rows[at] = row
        self._matrix = None

    def query(self, query_embeddings, n_results: int = 8,
              where: dict | None = None) -> dict:
        """Nearest records per query vector. One row per vector, so multi-query
        expansion gets a result per variant to merge."""
        queries = _unit(np.atleast_2d(np.asarray(query_embeddings,
                                                 dtype=np.float32)))
        kept = [i for i, metadata in enumerate(self.metadatas)
                if matches(metadata, where)]
        out: dict[str, list] = {'ids': [], 'documents': [], 'metadatas': [],
                                'distances': [], 'embeddings': []}
        matrix = self._unit_matrix()[kept] if kept else None
        take = max(0, min(n_results, len(kept)))
        for vector in queries:
            if matrix is None or not take:
                for key in out:
                    out[key].append([])
                continue
            similarities = matrix @ vector
            # Stable, so an embedder that scores everything identically — the
            # zero-vector baseline — still returns the same order every run.
            order = np.argsort(-similarities, kind='stable')[:take]
            rows = [kept[int(i)] for i in order]
            out['ids'].append([self.ids[r] for r in rows])
            out['documents'].append([self.documents[r] for r in rows])
            out['metadatas'].append([self.metadatas[r] for r in rows])
            out['distances'].append([1.0 - float(similarities[int(i)])
                                     for i in order])
            out['embeddings'].append([self._rows[r] for r in rows])
        return out

    def get(self, ids: list[str], include=('embeddings',)) -> dict:
        """The records named, in the order named, silently skipping ids this
        store does not hold — Chroma's behaviour, and the caller pairs ids with
        vectors by position, so a placeholder row would be a wrong vector."""
        out: dict[str, list] = {'ids': [], 'documents': [], 'metadatas': []}
        if 'embeddings' in include:
            out['embeddings'] = []
        for chunk_id in ids:
            at = self._at.get(chunk_id)
            if at is None:
                continue
            out['ids'].append(chunk_id)
            out['documents'].append(self.documents[at])
            out['metadatas'].append(self.metadatas[at])
            if 'embeddings' in out:
                out['embeddings'].append(self._rows[at])
        return out

    def drop(self) -> None:
        """Forget everything. Kept because the panel's rebuild path had it; with
        no database behind this, letting the object go achieves the same."""
        self.__init__(self.name)

    def _unit_matrix(self) -> np.ndarray:
        if self._matrix is None:
            self._matrix = _unit(np.vstack(self._rows).astype(np.float32))
        return self._matrix
