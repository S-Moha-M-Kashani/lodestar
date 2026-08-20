"""CLI-subscription chat backends: the model is whatever `claude`/`codex`
serves, no API key ever enters this repo. The binary is overridable by env var
(the LODESTAR_RCLONE_BIN idiom), which is what keeps this file offline: the
tests install a stub script and assert on what the wrapper does with its
output — parsing, tool-call extraction, hardening flags, timeouts — never on a
real model.

Both fixtures below are *captures*, not guesses. They were taken on 2026-08-15
from Claude Code 2.1.233 and codex-cli 0.147.0 by running

    claude -p 'Reply with exactly: pong' --output-format json --model sonnet
    codex exec --json 'Reply with exactly: pong'

and trimmed only of fields no wrapper reads (session ids, timings, per-model
cost breakdowns). Re-run those two commands before changing either constant: a
fixture invented to match a parser is a parser that has never met its input.
"""
import json
import stat
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from lodestar_brain.agent import LodestarAgent
from lodestar_brain.config import PROVIDER_MODELS, Settings
from lodestar_brain.llm import make_chat_model

# One JSON object on stdout. Note `usage.input_tokens: 2` — that is the
# *non-cached* input alone, and it cannot be the whole prompt: Claude Code ships
# a system prompt of its own, which is what the two cache counters hold. A turn
# reported as having read 2 tokens is off by four orders of magnitude, and this
# repo's rule about token figures is that nothing is fabricated (pricing.py
# returns None rather than a comforting zero), so the wrapper adds the cache
# counters back in.
FIXTURE_CLAUDE = json.dumps({
    'is_error': False, 'num_turns': 1, 'stop_reason': 'end_turn',
    'total_cost_usd': 0.18422819999999998,
    'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 29449,
              'cache_read_input_tokens': 24894, 'output_tokens': 4,
              'service_tier': 'standard'},
    'permission_denials': [], 'subtype': 'success', 'api_error_status': None,
    'result': 'pong', 'type': 'result'})

# JSON-lines on stdout, one event per line. The reply is the `text` of an
# `item.completed` event whose item is an `agent_message`; the usage arrives
# separately on `turn.completed`. Neither is the first line nor the last, which
# is the point of keeping all four: a wrapper that reads `head -1` or `tail -1`
# passes nothing here. Unlike Claude Code's, this `input_tokens` is taken at
# face value and nothing is summed — see the note where the usage is built.
FIXTURE_CODEX = '\n'.join(json.dumps(event) for event in [
    {'type': 'thread.started', 'thread_id': '01a0044a-3845-7b70-8857-a6e1c1478194'},
    {'type': 'turn.started'},
    {'type': 'item.completed',
     'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'pong'}},
    {'type': 'turn.completed',
     'usage': {'input_tokens': 19940, 'cached_input_tokens': 6912,
               'cache_write_input_tokens': 0, 'output_tokens': 5,
               'reasoning_output_tokens': 0}}])


def _stub(tmp_path, name, body):
    """Install an executable stand-in for a CLI: it swallows the prompt on
    stdin, records the argv it was handed, and then runs `body`.

    Recording the argv is what lets the hardening test assert on the command
    that was really executed rather than on a helper's return value.
    """
    script = tmp_path / name
    script.write_text('#!/bin/sh\n'
                      'cat > /dev/null\n'
                      f'printf \'%s\\n\' "$@" > "{script}.argv"\n'
                      + body + '\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _replies(tmp_path, name, *bodies):
    """A stub that prints one body per call, byte for byte, the last repeating.

    More than one because an agent turn calls the binary again after each tool
    result, and a stub that always asks for the same tool call would loop until
    the step limit. Deliberately `cat` of a file rather than `echo`: where
    /bin/sh is dash, echo expands the backslash escapes inside a JSON fixture
    and the wrapper is handed something that is not JSON at all — a failure that
    would only ever appear off this machine.
    """
    files = []
    for n, body in enumerate(bodies):
        fixture = tmp_path / f'{name}.{n}.out'
        fixture.write_text(body)
        files.append(fixture)
    count = tmp_path / f'{name}.calls'
    cases = '\n'.join(f'{n}) cat "{f}" ;;' for n, f in enumerate(files))
    return _stub(tmp_path, name,
                 f'n=$(cat "{count}" 2>/dev/null || echo 0)\n'
                 f'echo $((n + 1)) > "{count}"\n'
                 f'case "$n" in\n{cases}\n*) cat "{files[-1]}" ;;\nesac')


def _argv(tmp_path, name):
    """The argv the stub was actually invoked with."""
    return (tmp_path / f'{name}.argv').read_text().splitlines()


def _flagged(argv, flag, value=None):
    """Is `flag` in argv, followed by `value` when one is named?"""
    for i, arg in enumerate(argv):
        if arg == flag:
            return value is None or (i + 1 < len(argv) and argv[i + 1] == value)
    return False


# This is a unit test, and the most important one in this file.
def test_every_invocation_strips_the_subprocess_of_its_own_tools(tmp_path, monkeypatch):
    """Neither CLI is an inference endpoint: `claude -p` boots a whole Claude
    Code session with Bash, Edit, Read and MCP available, in whatever directory
    the brain is running in — which is this repository. The probe that pinned
    the fixtures above cost $0.18 and created 29,449 cache tokens for the word
    "pong"; that is the session's own system prompt and tool definitions.

    That matters because the prompt this wrapper sends is built from card notes,
    and card notes are a *measured* injection channel: `middleware/untrusted.py`
    records 3 of 12 hostile payloads obeyed, and all three failures were that
    channel. The fence protects a model. It does not protect a subprocess's
    permissions. Without these flags, "the model said something wrong" becomes
    "the model ran a command in the repository".

    So the flags are asserted against the argv the stub was really invoked with,
    not against a helper's return value: a flag that lives only in the code is
    one refactor away from being absent, and nothing else here would notice.
    """
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN',
                       _replies(tmp_path, 'claude', FIXTURE_CLAUDE))
    monkeypatch.setenv('BRAIN_CODEX_CLI_BIN',
                       _replies(tmp_path, 'codex', FIXTURE_CODEX))

    make_chat_model(Settings(llm_provider='claude-cli')).invoke(
        [HumanMessage(content='ping')])
    argv = _argv(tmp_path, 'claude')
    assert _flagged(argv, '--tools', ''), 'every built-in tool must be disabled'
    assert '--strict-mcp-config' in argv, 'no MCP server this repo did not name'
    assert _flagged(argv, '--setting-sources', ''), 'no project or user settings'
    assert '--system-prompt' in argv, 'replace the session prompt, never inherit'
    # --bare would also strip the session, and would break the whole point of
    # this backend: its own help says Anthropic auth becomes strictly
    # ANTHROPIC_API_KEY, so the subscription is never read.
    assert '--bare' not in argv

    make_chat_model(Settings(llm_provider='codex-cli')).invoke(
        [HumanMessage(content='ping')])
    argv = _argv(tmp_path, 'codex')
    assert _flagged(argv, '-s', 'read-only'), 'the sandbox may not write'
    assert '--ignore-user-config' in argv
    assert '--ignore-rules' in argv, 'no user or project execpolicy rules'
    assert '--ephemeral' in argv, 'a private board leaves no session file behind'


# This is a unit test, and it finishes the job the one above starts.
def test_the_subprocess_runs_in_a_scratch_directory_not_the_repository(
        tmp_path, monkeypatch):
    """The flags deny the subprocess its tools; this denies it its context.

    `claude -p` and `codex exec` both start in the process's working directory,
    which for the brain is this repository — next to `databases/real/`, and next
    to a `CLAUDE.md` and an `AGENTS.md` that each CLI reads as instructions
    addressed to it. Two distinct problems from one fact: whatever survives the
    hardening flags gets the repo as its blast radius, and the "system prompt"
    the wrapper so carefully replaces is joined by project context nobody chose
    to send.

    The security note in `llm_cli.py` names this and asks for exactly this fix —
    "a scratch working directory for the subprocess instead of the repository
    root". So the stub reports the directory it was actually run in, because a
    `cwd=` argument that a later refactor drops would leave every other test
    here passing.
    """
    where = tmp_path / 'claude.cwd'
    listing = tmp_path / 'claude.ls'
    # Both facts are reported from *inside* the subprocess, which is the only
    # place they can be read: the scratch directory is a TemporaryDirectory and
    # is gone by the time this test could look at it. It is also the stronger
    # measurement — this is the directory as the CLI itself sees it.
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _stub(
        tmp_path, 'claude', f'pwd > "{where}"\n'
                            f'ls -A > "{listing}"\n'
                            f'cat "{tmp_path}/fixture"'))
    (tmp_path / 'fixture').write_text(FIXTURE_CLAUDE)

    make_chat_model(Settings(llm_provider='claude-cli')).invoke(
        [HumanMessage(content='ping')])

    ran_in = Path(where.read_text().strip()).resolve()
    repo = Path(__file__).resolve().parents[2]
    assert ran_in != repo, 'the CLI must not be run in the repository root'
    assert repo not in ran_in.parents, 'nor anywhere inside the repository'
    # Empty, not merely elsewhere: a scratch directory that carried a CLAUDE.md
    # or an AGENTS.md would hand the subprocess instructions by another route,
    # and this backend's whole prompt discipline is that the system text is the
    # one the wrapper wrote.
    assert listing.read_text().strip() == '', 'the cwd carries no project context'


# This is a unit test.
def test_the_seam_builds_the_wrappers_and_still_rejects_unknowns(tmp_path, monkeypatch):
    from lodestar_brain.llm_cli import ClaudeCliChatModel, CodexCliChatModel
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _stub(tmp_path, 'claude', 'echo x'))
    monkeypatch.setenv('BRAIN_CODEX_CLI_BIN', _stub(tmp_path, 'codex', 'echo x'))
    assert isinstance(make_chat_model(Settings(llm_provider='claude-cli')),
                      ClaudeCliChatModel)
    assert isinstance(make_chat_model(Settings(llm_provider='codex-cli')),
                      CodexCliChatModel)
    with pytest.raises(ValueError):
        make_chat_model(Settings(llm_provider='gemini-cli'))
    # PROVIDER_MODELS has to move with the backend, or /agent/models answers
    # 'claude-cli' with an OpenRouter slug — the mismatch that dict exists to
    # prevent (test_config.py::test_choosing_the_backend_chooses_the_model...).
    # Codex's own default model is deliberately not pinned to a name: the owner
    # chose "whatever codex defaults to", and naming it here would freeze it.
    assert Settings(llm_provider='claude-cli').model == 'sonnet'
    assert Settings(llm_provider='codex-cli').model != PROVIDER_MODELS['openrouter']
    # The same rule as the local backend: a real credential must never leave for
    # somewhere it was not issued for, and a subscription CLI authenticates
    # itself. Nothing on the wrapper may carry the OpenRouter key.
    built = make_chat_model(Settings(llm_provider='claude-cli',
                                     openrouter_api_key='sk-real-secret'))
    assert 'sk-real-secret' not in json.dumps(built.model_dump(), default=str)


# This is a unit test.
def test_claude_cli_replies_are_parsed_with_usage(tmp_path, monkeypatch):
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN',
                       _replies(tmp_path, 'claude', FIXTURE_CLAUDE))
    llm = make_chat_model(Settings(llm_provider='claude-cli'))
    reply = llm.invoke([HumanMessage(content='ping')])
    assert reply.content == 'pong'
    assert reply.usage_metadata['output_tokens'] == 4
    # Cache reads and cache writes are input the turn paid for, so they are part
    # of input_tokens (LangChain's own convention) rather than a rounding error
    # the Assistant would show as a near-free turn.
    assert reply.usage_metadata['input_tokens'] == 2 + 29449 + 24894
    assert reply.usage_metadata['total_tokens'] == 2 + 29449 + 24894 + 4


# This is a unit test.
def test_codex_cli_replies_are_parsed_out_of_the_event_stream(tmp_path, monkeypatch):
    monkeypatch.setenv('BRAIN_CODEX_CLI_BIN',
                       _replies(tmp_path, 'codex', FIXTURE_CODEX))
    llm = make_chat_model(Settings(llm_provider='codex-cli'))
    reply = llm.invoke([HumanMessage(content='ping')])
    # The text is on the third of four lines and the usage on the fourth: a
    # wrapper reading one end of the stream cannot pass both of these.
    assert reply.content == 'pong'
    assert reply.usage_metadata['input_tokens'] == 19940
    assert reply.usage_metadata['output_tokens'] == 5


# This is a unit test.
def test_a_fenced_tool_call_becomes_a_real_tool_call(tmp_path, monkeypatch):
    body = json.dumps({'type': 'result', 'usage': {},
                       'result': 'On it.\n```tool_call\n'
                                 '{"name": "list_cards", "args": {"column_id": ""}}\n```'})
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _replies(tmp_path, 'claude', body))
    llm = make_chat_model(Settings(llm_provider='claude-cli')).bind_tools([])
    reply = llm.invoke([HumanMessage(content='what is on the board?')])
    assert reply.tool_calls and reply.tool_calls[0]['name'] == 'list_cards'
    assert reply.tool_calls[0]['args'] == {'column_id': ''}
    # The fence is an instruction to the model, not something the user asked to
    # read: what is left as content is the prose before it.
    assert reply.content == 'On it.'
    # A constant id would collide across the turns of one checkpointed thread,
    # where `add_messages` pairs on id and a ToolMessage names the call it
    # answers. Two calls, two ids.
    again = llm.invoke([HumanMessage(content='again')])
    assert reply.tool_calls[0]['id'] != again.tool_calls[0]['id']


# This is an integration test: one whole agent turn, driven by a stub binary.
def test_a_cli_backed_agent_actually_runs_the_tool_it_asks_for(tmp_path, monkeypatch):
    """The question five unit tests cannot answer: `create_agent` binds tools by
    calling `bind_tools`, and what this wrapper returns from it is not the
    `RunnableBinding` the framework's own models return. A backend that parses
    beautifully and cannot serve one agent turn is worse than an honest gap, so
    the turn is run here rather than reasoned about.
    """
    ran = []

    @tool
    def echo(text: str) -> dict:
        """Echo back."""
        ran.append(text)
        return {'echoed': text}

    asks = json.dumps({'type': 'result', 'usage': {},
                       'result': 'Looking.\n```tool_call\n'
                                 '{"name": "echo", "args": {"text": "ping"}}\n```'})
    answers = json.dumps({'type': 'result', 'usage': {}, 'result': 'done'})
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN',
                       _replies(tmp_path, 'claude', asks, answers))
    agent = LodestarAgent(settings=Settings(llm_provider='claude-cli'),
                          tools=[echo], system_prompt='sys')
    result = agent.run([{'role': 'user', 'content': 'echo ping'}])
    assert ran == ['ping'], 'the tool the model asked for never ran'
    assert result.reply == 'done'
    assert [step.tool for step in result.steps] == ['echo']


# This is a unit test.
def test_a_hung_cli_times_out_instead_of_hanging_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _stub(tmp_path, 'claude', 'sleep 30'))
    llm = make_chat_model(Settings(llm_provider='claude-cli'))
    llm.timeout = 0.5
    with pytest.raises(Exception, match='timed out|Timeout'):
        llm.invoke([HumanMessage(content='ping')])


# This is a unit test.
def test_a_failing_cli_says_why_instead_of_naming_an_exit_code(tmp_path, monkeypatch):
    """An expired subscription is the most likely real failure of this backend,
    and `subprocess.run(check=True)` reports it as "returned non-zero exit status
    1" with the reason discarded. The same reasoning as the tool-error handler
    returning `str(exc)`: what reaches the user has to name the problem.
    """
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN',
                       _stub(tmp_path, 'claude',
                             'echo "Invalid API key · Please run /login" >&2\nexit 1'))
    llm = make_chat_model(Settings(llm_provider='claude-cli'))
    with pytest.raises(Exception, match='/login'):
        llm.invoke([HumanMessage(content='ping')])
