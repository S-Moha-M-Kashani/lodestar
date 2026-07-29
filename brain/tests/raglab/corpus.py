"""Fixture access: the year of diary sessions and its ground-truth questions."""
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'
DIARY_PATH = FIXTURES / 'diary_year_fa.json'
GROUND_TRUTH_PATH = FIXTURES / 'diary_year_fa_groundtruth.json'


def load_diary(path: Path = DIARY_PATH) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_ground_truth(path: Path = GROUND_TRUTH_PATH) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def date_int(date: str) -> int:
    """'2026-03-10' -> 20260310. Chroma metadata filters compare numbers, not
    date strings, so every chunk carries this and time filters use $gte/$lte."""
    return int(date.replace('-', ''))


def session_text(session: dict) -> str:
    """One session as plain dialogue text, role-tagged so a chunk read in
    isolation still shows who said what."""
    lines = []
    for message in session['messages']:
        speaker = 'کاربر' if message['role'] == 'user' else 'دستیار'
        lines.append(f"{speaker}: {message['content']}")
    return '\n'.join(lines)


def sessions_by_id(diary: dict) -> dict[str, dict]:
    return {s['session_id']: s for s in diary['sessions']}


def evidence_texts(sessions: dict[str, dict], question: dict) -> list[str]:
    """The full text of every message the ground truth cites as evidence.

    Used as `reference_contexts` for RAGAS's string-similarity context metrics.
    The verbatim quote is the more precise reference, but those metrics compare
    whole strings — a 60-character quote against a 900-character chunk scores as
    no match however perfectly the quote is contained in it. The message is the
    comparable unit; `metrics.quote_recall` is where quote-level precision is
    measured instead."""
    out: list[str] = []
    for ev in question.get('evidence', []):
        session = sessions.get(ev['session_id'])
        if not session:
            continue
        for index in ev.get('message_indices', []):
            if 0 <= index < len(session['messages']):
                out.append(session['messages'][index]['content'])
    return out or [ev['quote'] for ev in question.get('evidence', [])]


def evidence_sessions(question: dict) -> list[str]:
    """Distinct evidence session ids, in the order the ground truth lists them."""
    seen: list[str] = []
    for ev in question.get('evidence', []):
        if ev['session_id'] not in seen:
            seen.append(ev['session_id'])
    return seen
