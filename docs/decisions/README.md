# Design records

One record per major feature: what was built, **why it was built that way**, and
what was rejected. Each is written before the code and kept afterwards as the
record — which is why several carry a dated amendment saying where reality
disagreed with the plan. Nothing here is edited to look right in hindsight.

Filenames are `YYYY-MM-DD-<subject>-design.md`, dated by the day the decision was
made rather than the day it shipped.

**The annotated index — one line per record — is in
[`../../ARCHITECTURE.md`](../../ARCHITECTURE.md#the-design-records),** beside the
architecture the records explain. It lives in one place on purpose: two indexes
of the same list drift, and this repository has a test whose whole job is to
catch documentation that has drifted from what it describes.

This directory was `docs/superpowers/specs/` until 2026-09-02. It was renamed
with `git mv`, so `git log --follow` still reaches every earlier version;
"superpowers" was the name of a tool, not a word a reader of this project has any
reason to know.

Implementation plans are not kept here. A plan is a checklist with `- [ ]` boxes,
and once the work has shipped a finished checklist is noise that still looks
executable, so plans are deleted rather than archived — the reasoning they were
built from is in the record beside them.
