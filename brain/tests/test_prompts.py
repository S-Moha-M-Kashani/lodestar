"""The exact words sent to a model, pinned character for character.

The gate's user message is welded to the `[n]` listing format its own parse
regex expects, and separating a prompt from its parser is how the two drift.
The coach prompt is reviewed as prose in `test_widget2.py` and
`test_guardrails.py`. The recap and the saved-notes header left with the tools
that sent them.
"""
from __future__ import annotations

from lodestar_brain.retrieval.gate import GATE_USER_TEMPLATE


# This is a unit test.
def test_the_gate_prompt_still_says_what_it_said():
    """Deliberately brittle, and that is the whole point of it.

    This string is what the grader reads. The gate test proves every candidate
    reaches the listing; it would not notice the header drifting by a word.

    So editing the prompt must fail this test, and the editor updates it in the
    same change. The blank line is what separates the question from the numbered
    excerpts, and the parse regex in the same module depends on it.
    """
    assert GATE_USER_TEMPLATE == 'Question: {query}\n\n{listing}'
