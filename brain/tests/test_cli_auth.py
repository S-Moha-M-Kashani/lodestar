"""Signing a CLI subscription in from the Assistant, for a brain in a container.

The two CLI chat backends spend a subscription that lives *where the brain
runs*. Natively that is the owner's own account and there is nothing to do —
`codex login` was typed into a terminal months ago. Under Docker the brain runs
somewhere with no terminal, no browser and no login of anybody's, and until this
existed the honest answer in `docs/cli-backends.md` was "not under Docker".

`codex login --device-auth` is what closes that: it prints a URL and a one-time
code, waits, and writes its own `auth.json` when the code is approved on any
other device. So the Assistant can show the code and the container can end up
logged in, with no port published, no browser inside it and no credential ever
travelling through this repo.

This file is offline for the same reason `test_llm_cli.py` is: both binaries are
named by env var (`BRAIN_CODEX_CLI_BIN`, `BRAIN_CLAUDE_CLI_BIN` — the
LODESTAR_RCLONE_BIN idiom), so a stub script stands in for the real CLI and the
tests assert on what the brain does with its output. Nothing here reaches
OpenAI, Anthropic or a subscription.
"""
import stat
import time

import pytest
from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app

# Byte-for-byte what `codex login --device-auth` printed on 2026-09-17, from
# codex-cli 0.154.0, captured by running it against a scratch CODEX_HOME. The
# colour codes are the point of keeping it verbatim: the URL and the code both
# arrive wrapped in them, so a parser written against a cleaned-up copy of this
# banner would hand the browser an ANSI escape inside a link.
DEVICE_BANNER = (
    'Welcome to Codex [v\x1b[90m0.154.0\x1b[0m]\n'
    "\x1b[90mOpenAI's command-line coding agent\x1b[0m\n"
    '\n'
    'Follow these steps to sign in with ChatGPT using device code '
    'authorization:\n'
    '\n'
    '1. Open this link in your browser and sign in to your account\n'
    '   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n'
    '\n'
    '2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n'
    '   \x1b[94mI2VL-BGRK4\x1b[0m\n'
    '\n'
    '\x1b[90mContinue only if you started this login in Codex.\x1b[0m\n')

CODE = 'I2VL-BGRK4'
DEVICE_URL = 'https://auth.openai.com/codex/device'


def _codex_stub(tmp_path):
    """A stand-in `codex` that answers the two subcommands the brain uses.

    `login status` exits 0 only once an `approved` marker exists, which is this
    stub's whole model of a subscription: signed out, then signed in, with the
    test deciding when. `login --device-auth` prints the captured banner and
    then waits for that marker exactly as the real one waits for the code to be
    entered — the brain must not report a login as finished while the CLI is
    still holding the door open.

    Every invocation is counted, because "one login at a time" is a claim about
    how many processes were started and cannot be checked any other way.
    """
    approved = tmp_path / 'approved'
    calls = tmp_path / 'device-auth.calls'
    banner = tmp_path / 'banner.txt'
    banner.write_text(DEVICE_BANNER)
    script = tmp_path / 'codex'
    script.write_text(
        '#!/bin/sh\n'
        'case "$2" in\n'
        f'  status) test -f "{approved}" ;;\n'
        '  --device-auth)\n'
        f'    n=$(cat "{calls}" 2>/dev/null || echo 0)\n'
        f'    echo $((n + 1)) > "{calls}"\n'
        f'    cat "{banner}"\n'
        f'    while [ ! -f "{approved}" ]; do sleep 0.05; done\n'
        '    exit 0 ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), approved, calls


def _app(monkeypatch, tmp_path, codex=None, claude=None):
    monkeypatch.setenv('BRAIN_CODEX_CLI_BIN', codex or str(tmp_path / 'no-codex'))
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', claude or str(tmp_path / 'no-claude'))
    return TestClient(create_app(Settings(llm_provider='fake', embedder='fake',
                                          transcriber='fake',
                                          board_api_url='http://board.test')))


def _wait(client, ready, tries=100):
    """Poll `GET /agent/cli-auth` until `ready(body)`, then hand that body back.

    The login is a subprocess the brain is holding open, so what it has noticed
    is a question about wall-clock time. Polling is what the browser does too,
    for the same reason.
    """
    for _ in range(tries):
        body = client.get('/agent/cli-auth').json()
        if ready(body):
            return body
        time.sleep(0.05)
    pytest.fail(f'the login never reached the expected state: {body}')


def _codex(body):
    return body['backends']['codex-cli']


# This is an integration test.
def test_a_device_login_hands_back_the_code_the_cli_printed(tmp_path, monkeypatch):
    """The whole feature, from the browser's side: one POST, and what comes back
    is a link to open and a code to type, both clean of the colour codes the CLI
    wrapped them in.

    Read off the CLI's own output rather than from a constant here, because the
    code changes every login and the URL is OpenAI's to move. A brain that
    printed a remembered URL beside a live code would be a login that cannot
    work and cannot say why.
    """
    binary, _, _ = _codex_stub(tmp_path)
    client = _app(monkeypatch, tmp_path, codex=binary)
    try:
        res = client.post('/agent/cli-auth/start', json={'backend': 'codex-cli'})
        assert res.status_code == 200
        assert res.json()['url'] == DEVICE_URL
        assert res.json()['code'] == CODE

        # And the same login is what the status route reports, so a browser that
        # was repainted between the click and the answer still finds the code
        # rather than having to start a second one.
        pending = client.get('/agent/cli-auth').json()['pending']
        assert pending['backend'] == 'codex-cli' and pending['code'] == CODE
    finally:
        client.post('/agent/cli-auth/cancel')


# This is an integration test.
def test_a_second_start_joins_the_login_already_in_flight(tmp_path, monkeypatch):
    """One login at a time, and a second press of the button is not a second
    one.

    Two `codex login --device-auth` processes mean two one-time codes, and the
    browser can only show one of them: the user would be reading a code that the
    process which eventually writes `auth.json` is not waiting for. Asserted on
    the number of processes started, not on the code that came back — an
    implementation that spawned a second CLI and then returned the first code
    would pass that weaker claim while leaving an orphan waiting for a code
    nobody will ever type.
    """
    binary, _, calls = _codex_stub(tmp_path)
    client = _app(monkeypatch, tmp_path, codex=binary)
    try:
        first = client.post('/agent/cli-auth/start',
                            json={'backend': 'codex-cli'}).json()
        second = client.post('/agent/cli-auth/start',
                             json={'backend': 'codex-cli'}).json()
        assert first['code'] == second['code'] == CODE
        assert calls.read_text().strip() == '1'
    finally:
        client.post('/agent/cli-auth/cancel')


# This is an integration test.
def test_an_approved_login_is_reported_signed_in_and_the_code_is_dropped(
        tmp_path, monkeypatch):
    """The end of the flow, which is the part the picker depends on.

    Before approval the backend is installed and signed out; after it the status
    route says signed in and carries no pending code — a code left on screen
    after it has been used is an instruction to do something twice.

    `signed_in` is read from `codex login status` rather than from the login
    process having exited, because those are different facts: a login that fails
    also exits.
    """
    binary, approved, _ = _codex_stub(tmp_path)
    client = _app(monkeypatch, tmp_path, codex=binary)
    try:
        before = client.get('/agent/cli-auth').json()
        assert _codex(before)['installed'] is True
        assert _codex(before)['signed_in'] is False

        client.post('/agent/cli-auth/start', json={'backend': 'codex-cli'})
        approved.touch()

        after = _wait(client, lambda body: _codex(body)['signed_in'] is True)
        assert after['pending'] is None
    finally:
        client.post('/agent/cli-auth/cancel')


# This is an integration test.
def test_a_backend_with_no_binary_is_not_offered_a_login_it_cannot_run(
        tmp_path, monkeypatch):
    """`served_models`' honesty rule, one door further out: a brain that cannot
    find the binary must say so rather than starting a login that will fail with
    nothing on screen to explain it.

    A refusal rather than a 500, because the browser has to be able to tell "this
    machine has no codex" apart from "the brain is broken" — the first is a
    sentence to show the user and the second is not.
    """
    client = _app(monkeypatch, tmp_path)
    body = client.get('/agent/cli-auth').json()
    assert _codex(body)['installed'] is False
    assert _codex(body)['signed_in'] is False

    res = client.post('/agent/cli-auth/start', json={'backend': 'codex-cli'})
    assert res.status_code == 400
    # And nothing was left running behind the refusal: a login the brain is
    # holding open for a binary it cannot find would show the browser a code
    # that no process is waiting for.
    assert client.get('/agent/cli-auth').json()['pending'] is None


# This is an integration test.
def test_the_claude_subscription_token_is_taken_write_only(tmp_path, monkeypatch):
    """Claude Code has no device login, and on macOS its login is not even a
    file — it is the system keychain, which no container can read. So the
    container is handed a subscription token instead (`claude setup-token` on
    the machine that has the keychain), typed into the same drawer as the
    OpenRouter key and under that route's contract, which this one repeats
    deliberately:

    - no response from either route may ever carry the token;
    - the status is yes or no, never the value;
    - and an empty save restores what the brain booted with, so a stack
      configured by env cannot be un-configured by an accidental save.
    """
    token = 'sk-ant-oat01-e2e-not-a-real-token'
    claude = tmp_path / 'claude'
    claude.write_text('#!/bin/sh\nexit 0\n')
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    client = _app(monkeypatch, tmp_path, claude=str(claude))

    res = client.post('/agent/cli-auth/token',
                      json={'backend': 'claude-cli', 'token': f'  {token}  '})
    assert res.status_code == 200
    assert res.json() == {'configured': True}
    assert token not in res.text

    body = client.get('/agent/cli-auth')
    assert body.json()['backends']['claude-cli']['signed_in'] is True
    assert token not in body.text

    cleared = client.post('/agent/cli-auth/token',
                          json={'backend': 'claude-cli', 'token': ''})
    assert cleared.json() == {'configured': False}
