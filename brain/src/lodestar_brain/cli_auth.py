"""Signing a CLI subscription in from the Assistant, for a brain that has no
terminal.

`llm_cli.py` answers a turn by running `claude` or `codex`, which works because
the machine the brain runs on has already logged in to one of them. Under Docker
that sentence has no subject: the container has no terminal, no browser and
nobody's login, so both backends were unreachable there however they were
configured — which `docs/cli-backends.md` said outright until this module
existed.

**Codex has a device login and that is the whole reason this is possible.**
`codex login --device-auth` prints a URL and a one-time code, waits, and writes
its own `auth.json` when the code is approved from any browser anywhere. So the
container needs no browser, no published port and no host credential handed in:
the Assistant shows the code, the owner approves it on a device they already
have, and the container ends up signed in. Nothing here ever reads, stores or
forwards a credential — this module runs a program and reads what it printed.

**Claude has no such flow, and the asymmetry is the platform's, not a gap here.**
On macOS Claude Code keeps its login in the system keychain, which no Linux
container can read and no mount can share; on Linux it is a file inside the
config directory. The one form of that credential a container can be *given* is
a long-lived subscription token (`claude setup-token`, run where the keychain
is), so the route that takes one is a paste field rather than a button, under
exactly the contract the OpenRouter key already has: write-only, yes-or-no
status, never echoed.

Both binaries are named by env var (`BRAIN_CODEX_CLI_BIN`,
`BRAIN_CLAUDE_CLI_BIN` — the `LODESTAR_RCLONE_BIN` idiom), which is what keeps
`brain/tests/test_cli_auth.py` offline: a stub script stands in for the CLI and
the tests assert on what this module does with its output.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .llm import CLI_PROVIDERS, _cli_binary

# The backends this module can sign in, which is not the same set as the ones
# that can *answer* — that is `CLI_PROVIDERS`, and the difference is the point.
# A backend missing here is one whose login cannot be driven from a web page,
# and saying so is better than a button that opens a flow nobody can finish.
DEVICE_LOGIN = ('codex-cli',)

# The banner says "expires in 15 minutes". Nothing here parses that sentence —
# a number read out of a CLI's prose is a number that moves when the prose does
# — so this is the brain's own patience, deliberately a little longer: the worst
# outcome of being late is one dead code on screen, and the worst outcome of
# being early is killing a login the owner is halfway through approving.
CODE_TTL = 16 * 60
# How long to wait for the CLI to print its banner. It is a network round trip
# to the device-code endpoint, not a model call, so it is short — and a start
# that hangs must fail as a start rather than as a code that never arrives.
BANNER_TIMEOUT = 60.0
# `codex login status` is a local file read behind a subprocess spawn. Anything
# longer than this is a broken binary, and the status route is asked on every
# open of the settings drawer.
STATUS_TIMEOUT = 15.0

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_URL = re.compile(r'https://\S+')


def _plain(text: str) -> str:
    """The CLI's output with its colour codes removed.

    Load-bearing rather than cosmetic: codex wraps both the URL and the code in
    them, so a parser reading the raw bytes hands the browser a link with an
    escape sequence inside it and a code with one on each end.
    """
    return _ANSI.sub('', text)


def parse_device_login(output: str) -> dict | None:
    """The URL to open and the code to type, or None while neither has printed.

    Read out of the CLI's own output and never from a constant here: the code is
    different every login and the URL is OpenAI's to move. A brain that showed a
    remembered URL beside a live code would be a login that cannot work and
    cannot say why.
    """
    text = _plain(output)
    url = _URL.search(text)
    if not url:
        return None
    # One rule and not two: the line after the CLI's own "one-time code"
    # sentence. A second rule — a regex for the code's *shape* — was written and
    # taken out again in the same pass: it would have been a branch no test
    # exercises, guarding against a reworded banner that this rule fails loudly
    # on anyway ("codex started no device login", with what it did print).
    code = ''
    lines = text.splitlines()
    for n, line in enumerate(lines):
        if 'one-time code' not in line.lower():
            continue
        for candidate in lines[n + 1:]:
            if candidate.strip():
                code = candidate.strip()
                break
        break
    if not code:
        return None
    return {'url': url.group(0).rstrip('.,'), 'code': code}


def _signed_in_codex(binary: str) -> tuple[bool, str]:
    """What `codex login status` says, which is the only thing that knows.

    Asked of the CLI rather than inferred from a file on disk: `auth.json` being
    present is not the same fact as the subscription still being good, and this
    answer is what the sign-in button appears and disappears on.
    """
    try:
        done = subprocess.run([binary, 'login', 'status'], capture_output=True,
                              text=True, timeout=STATUS_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f'codex could not be asked: {exc}'
    if done.returncode == 0:
        return True, (_plain(done.stdout).strip().splitlines() or ['signed in'])[0]
    return False, 'not signed in yet'


def _signed_in_claude(settings: Settings) -> tuple[bool | None, str]:
    """Whether Claude Code has a credential here — and `None` when that cannot
    be known, which is the common case on a Mac.

    Three answers rather than two, because two would mean lying in one of them:
    a token configured for this brain is a yes, a credentials file beside the
    binary is a yes, and everything else is genuinely unknown — the login may be
    sitting in the operating system's keychain, which nothing in this process
    can read. Reporting that as "signed out" would send the owner to log in
    again for no reason.
    """
    if settings.claude_oauth_token:
        return True, 'a subscription token is set'
    root = os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude')
    if (Path(root) / '.credentials.json').exists():
        return True, 'signed in on this machine'
    # True wherever this runs, which the earlier wording was not: it named the
    # macOS keychain, and read as nonsense from inside a Linux container that
    # has no keychain and simply has not been given anything. Both facts it can
    # actually check are stated, and the third is left as the possibility it is.
    return None, ('cannot be checked from here — no token is set and there is '
                  'no credentials file, and a login may be held in a keychain '
                  'this cannot read')


def cli_state(backend: str, settings: Settings) -> dict:
    """One backend's two facts: is the binary here, and is it signed in.

    `served_models`' honesty rule, one door further out. `installed` is what
    `cli_backends()` already reports to the picker; `signed_in` is what this
    module adds, and the picker deliberately does **not** read it — a backend
    that is installed is still offered, with the sign-in control beside it
    saying what is missing. Hiding the option would leave the owner a brain they
    could not sign in to.
    """
    binary = _cli_binary(backend)
    if shutil.which(binary) is None:
        return {'backend': backend, 'installed': False, 'signed_in': False,
                'detail': f'{binary} is not installed where the brain runs'}
    signed_in, detail = (_signed_in_codex(binary) if backend == 'codex-cli'
                         else _signed_in_claude(settings))
    return {'backend': backend, 'installed': True, 'signed_in': signed_in,
            'detail': detail}


@dataclass
class _Pending:
    backend: str
    url: str
    code: str
    started: float
    process: subprocess.Popen


class CliLogin:
    """The one device login a brain may have in flight, and its status.

    Single-flight on purpose. Two `codex login --device-auth` processes mean two
    one-time codes, and a browser can only show one of them — so the owner would
    be reading a code that the process which eventually writes `auth.json` is
    not waiting for. A second press of the button therefore joins the login
    already running rather than starting another.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: _Pending | None = None

    def state(self, settings: Settings) -> dict:
        backends = {backend: cli_state(backend, settings)
                    for backend in sorted(CLI_PROVIDERS)}
        with self._lock:
            self._settle(backends)
            pending = self._pending
            return {'backends': backends,
                    'pending': None if pending is None else
                    {'backend': pending.backend, 'url': pending.url,
                     'code': pending.code}}

    def start(self, backend: str, settings: Settings) -> dict:
        """Begin a device login, or hand back the one already running."""
        if backend not in DEVICE_LOGIN:
            raise ValueError(f'{backend} has no login this page can drive; '
                             f'paste a subscription token instead')
        state = cli_state(backend, settings)
        if not state['installed']:
            raise ValueError(state['detail'])
        with self._lock:
            if self._pending is not None and self._pending.process.poll() is None:
                pending = self._pending
                return {'backend': pending.backend, 'url': pending.url,
                        'code': pending.code}
            self._pending = self._spawn(backend, _cli_binary(backend))
            pending = self._pending
        return {'backend': pending.backend, 'url': pending.url,
                'code': pending.code}

    def cancel(self) -> dict:
        with self._lock:
            self._drop()
        return {'ok': True}

    # -- internals ---------------------------------------------------------

    def _spawn(self, backend: str, binary: str) -> _Pending:
        """Run the CLI and read until it has printed the code.

        stderr is merged into stdout because the banner is prose rather than
        data and neither CLI promises which stream it lands on; the process is
        then left running, since it is the thing that will write `auth.json`
        when the code is approved.
        """
        process = subprocess.Popen(
            [binary, 'login', '--device-auth'], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        seen, deadline = '', time.monotonic() + BANNER_TIMEOUT
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            seen += line
            if (found := parse_device_login(seen)):
                return _Pending(backend=backend, url=found['url'],
                                code=found['code'], started=time.monotonic(),
                                process=process)
        _kill(process)
        raise RuntimeError(
            f'{binary} started no device login: {_plain(seen).strip() or "no output"}')

    def _settle(self, backends: dict) -> None:
        """Drop the pending login once it can no longer be acted on.

        Three ways it ends, and the first is the one that matters: the backend
        reports itself signed in, which is read from the CLI rather than from
        this process having exited — a login that *fails* also exits. A code
        left on screen after it has been used is an instruction to do something
        that cannot work.
        """
        pending = self._pending
        if pending is None:
            return
        if backends.get(pending.backend, {}).get('signed_in') is True:
            return self._drop()
        if pending.process.poll() is not None:
            return self._drop()
        if time.monotonic() - pending.started > CODE_TTL:
            return self._drop()

    def _drop(self) -> None:
        if self._pending is not None:
            _kill(self._pending.process)
            self._pending = None


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if process.stdout is not None:
        process.stdout.close()
