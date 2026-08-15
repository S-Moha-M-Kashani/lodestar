"""CLI-subscription chat backends: the model is whatever `claude`/`codex`
serves, no API key ever enters this repo. The binary is overridable by env var
(the LODESTAR_RCLONE_BIN idiom), which is what keeps this file offline: the
tests install a stub script and assert on what the wrapper does with its
output — parsing, tool-call extraction, timeouts — never on a real model.

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

import pytest
from langchain_core.messages import HumanMessage

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
# passes nothing here. Unlike Claude Code's, this `input_tokens` is the whole
# input with `cached_input_tokens` as a subset of it, so it is taken verbatim
# and nothing is summed.
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
    """Install an executable stand-in for a CLI: it swallows the prompt on stdin
    and then runs `body`."""
    script = tmp_path / name
    script.write_text('#!/bin/sh\ncat > /dev/null\n' + body + '\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _prints(tmp_path, name, text):
    """A stub that prints `text` byte for byte. Deliberately `cat` of a file and
    not `echo`: where /bin/sh is dash, echo expands the \\n escapes inside a JSON
    fixture, and the wrapper is then handed something that is not JSON at all —
    a failure that would only appear off this machine."""
    fixture = tmp_path / f'{name}.out'
    fixture.write_text(text)
    return _stub(tmp_path, name, f'cat "{fixture}"')


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
                       _prints(tmp_path, 'claude', FIXTURE_CLAUDE))
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
                       _prints(tmp_path, 'codex', FIXTURE_CODEX))
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
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _prints(tmp_path, 'claude', body))
    llm = make_chat_model(Settings(llm_provider='claude-cli')).bind_tools([])
    reply = llm.invoke([HumanMessage(content='what is on the board?')])
    assert reply.tool_calls and reply.tool_calls[0]['name'] == 'list_cards'
    assert reply.tool_calls[0]['args'] == {'column_id': ''}
    # The fence is an instruction to the model, not something the user asked to
    # read: what is left as content is the prose before it.
    assert reply.content == 'On it.'


# This is a unit test.
def test_a_hung_cli_times_out_instead_of_hanging_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv('BRAIN_CLAUDE_CLI_BIN', _stub(tmp_path, 'claude', 'sleep 30'))
    llm = make_chat_model(Settings(llm_provider='claude-cli'))
    llm.timeout = 0.5
    with pytest.raises(Exception, match='timed out|Timeout'):
        llm.invoke([HumanMessage(content='ping')])
