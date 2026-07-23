"""Deterministic offline provider for unit tests, e2e, and CI.

Scripted mode pops pre-baked turns in order. Heuristic mode (no script):
- a user message starting with 'add:' triggers one create_question tool call,
  then a '... created ...' reply once the tool result is in the transcript;
- anything else echoes back as 'FAKE: <text>'.
"""
from .base import AssistantTurn, ToolCall


class FakeProvider:
    def __init__(self, script: list[AssistantTurn] | None = None):
        self.script = list(script) if script is not None else None

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None) -> AssistantTurn:
        if self.script is not None:
            return self.script.pop(0)
        last_user = next((m for m in reversed(messages) if m['role'] == 'user'),
                         {'content': ''})
        text = (last_user.get('content') or '').strip()
        tool_ran = any(m['role'] == 'tool' for m in messages)
        if text.lower().startswith('add:'):
            title = text[4:].strip()
            if not tool_ran:
                return AssistantTurn(tool_calls=[ToolCall(
                    id='fake-1', name='create_question', arguments={'title': title})])
            return AssistantTurn(content=f'FAKE: created "{title}"')
        return AssistantTurn(content=f'FAKE: {text}')
