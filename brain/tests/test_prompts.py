"""The exact words sent to a model, pinned character for character.

Every prompt in this package is an UPPER_CASE constant beside the machinery
that sends it — deliberately not gathered into a `prompts.py`, because the
gate's user message is welded to the `[n]` listing format its own parse regex
expects, and separating a prompt from its parser is how the two drift.

`SYSTEM_PROMPT` is not here: it is reviewed as prose and its contract is
asserted by meaning in `test_guardrails.py` ("cannot delete" travels with the
way out; each lookup has a role). This file is the other three, which no test
in this suite asserted a character of until 2026-08-15.
"""
from __future__ import annotations

from lodestar_brain.middleware.memory import NOTES_HEADER
from lodestar_brain.retrieval.gate import GATE_USER_TEMPLATE
from lodestar_brain.tools.recap import SUMMARY_PROMPT


# This is a unit test.
def test_the_three_extracted_prompts_still_say_what_they_said():
    """Deliberately brittle, and that is the whole point of it.

    These three strings are what a model actually reads. Nothing else in the
    suite asserts a character of any of them: the recap test only proves the
    user's own messages reach the summary prompt (FakeChat echoes it back), the
    gate test only proves every candidate reaches the listing, and the fenced
    notes test only proves a fact lands between the fence markers. All three
    headers could have drifted by a word — or, on the day they were lifted into
    constants, by a whole clause — with the suite green.

    So editing a prompt must fail this test, and the editor updates it in the
    same commit. That is a deliberate act rather than a side effect, which is
    the only property worth having here.

    What this does not pin: that the call sites still *use* these constants.
    The tests named above exercise those paths and would notice a prompt that
    lost its message list or its listing, but a call site that quietly went
    back to a literal would pass everything. The constant is the convention;
    this is the guard on its words.
    """
    # tools/recap.py — the user's own messages are appended after the colon.
    assert SUMMARY_PROMPT == (
        "Summarize in a few sentences what was on the user's mind in these "
        'messages, in their own terms:\n')

    # middleware/memory.py — the agent's notes, introduced before the fence
    # that puts them in the data channel. "may be out of date" is load-bearing:
    # this store has no temporal validity, so the warning is all there is.
    assert NOTES_HEADER == (
        '\n\nNotes you saved about this board in earlier conversations. They '
        "are your own notes, not the user's record, and they may be out of "
        'date — check before relying on one.\n')

    # retrieval/gate.py — welded to the parse regex in the same module: the
    # blank line is what separates the question from the numbered excerpts.
    assert GATE_USER_TEMPLATE == 'Question: {query}\n\n{listing}'
