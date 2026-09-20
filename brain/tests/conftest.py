"""One helper the source-scanning tests share.

Several tests in this suite make a claim by *reading the source*, because the
thing they guard against is invisible until the day it is used: a second engine
list, a second card-rendering site, a write method on a module that must only
read. `test_guardrails.py` established the idiom and this is not a new one.

What is new is that those scans now run over modules carrying an
`Alternatives considered` note — the format `brain/CLAUDE.md` requires of any
module that hand-rolls something a library could have done. Such a note has to
be free to say *why* sqlite was the wrong answer, and a scan reading the whole
file cannot tell that sentence from an import. The reader's own guard failed on
exactly that, on its own prose, having correctly named no writer at all.

So the scan reads code. Comments never survive `ast.unparse`, and every string
standing alone as a statement is dropped — which is the right rule and a wider
one than "the docstring". This project's `Alternatives considered` note is a
*trailing* block, after the last function, so it is not a docstring at all;
dropping only docstrings left the reader's guard failing on the very paragraph
explaining why sqlite was rejected. A string nothing reads is prose by
definition, wherever it sits: prose argues about alternatives, code chooses one,
and only the choice is under test.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _is_prose(node: ast.stmt) -> bool:
    """A string standing alone as a statement — a docstring, or a trailing note.

    Nothing can read it, so it cannot be part of what a module *does*. A string
    that is read — a tool's description, a prompt, a `Field(description=…)` —
    is an argument or an assignment and is kept, which is deliberate: those
    reach the model, and a scan of what the model is told is a fair scan.
    """
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def code_of(module) -> str:
    """One module's source with its prose removed — imports, names and calls.

    Takes the module object rather than a path, so a caller that renames a file
    cannot silently start scanning nothing.
    """
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if isinstance(body, list):
            node.body = [stmt for stmt in body if not _is_prose(stmt)] or body
    return ast.unparse(tree)
