# Security policy

Lodestar holds a private life on one machine. This file says what is defended,
what is not, and how to tell me about a hole in either.

## Supported version

The newest release point on `master` — the tag `package.json` names. There are
no maintained older branches, so a fix lands as the next release and nothing is
back-ported.

## The boundary, in one paragraph

The board answers on `127.0.0.1` only, refuses any `Host` header outside an
exact allowlist, and puts every board and chat route behind a password-backed
session that fails closed — a missing or malformed credential stops the process
at boot rather than serving without one. The model key lives only in the brain's
environment and never reaches the browser. What that does and does not protect,
and what to do instead of exposing a port, is spelled out in
[`docs/security.md`](docs/security.md).

## What is known and measured, not claimed

The prompt-injection defence is a fence around untrusted text, not a classifier,
and its effectiveness is a published number rather than a promise: **3 of 12
hostile payloads got through on `openai/gpt-5-nano`**, all three in the
card-notes channel. The measurement lives with the code that makes the claim
(`brain/src/lodestar_brain/untrusted.py`) and the eval that produces it is
`brain/tests/evals/test_injection.py`.

## Reporting a vulnerability

Email **s.moha.m.kashani@gmail.com** with a subject beginning `Lodestar
security`. Please include what you did, what happened, and the version or commit
you saw it on. Do not open a public issue for anything that could be used
against a running copy.

This is a personal project with one maintainer: expect a reply within a week,
and no bounty — there is no money behind this, only my thanks and credit in the
release note if you want it.
