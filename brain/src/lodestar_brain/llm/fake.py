"""Deterministic offline chat model for unit tests, e2e, and CI.

Scripted mode pops pre-baked messages in order. Heuristic mode (no script):
- a user message starting with 'add:' yields one create_question tool call,
  then a '... created ...' reply once a ToolMessage is in the transcript;
- anything else echoes back as 'FAKE: <text>'.

The 'FAKE: ...' strings and the 'add:' prefix are asserted by
tests/e2e_test.py (lines 955, 961, 1001) — do not change them.
"""
from typing import Any, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # Some providers return content blocks; only the text parts matter here.
    return ''.join(part.get('text', '') for part in content
                   if isinstance(part, dict))


class FakeChat(BaseChatModel):
    script: Optional[list[BaseMessage]] = None

    @property
    def _llm_type(self) -> str:
        return 'fake'

    def bind_tools(self, tools: Sequence, **kwargs: Any) -> 'FakeChat':
        """create_agent binds tools to the model; this fake ignores them and
        decides what to call from the transcript alone."""
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        if self.script:
            return self.script.pop(0)
        last_user = next((m for m in reversed(messages)
                          if isinstance(m, HumanMessage)), None)
        text = _text(last_user).strip() if last_user is not None else ''
        tool_ran = any(isinstance(m, ToolMessage) for m in messages)
        if text.lower().startswith('add:'):
            title = text[4:].strip()
            if not tool_ran:
                return AIMessage(content='', tool_calls=[
                    {'name': 'create_question', 'args': {'title': title},
                     'id': 'fake-1'}])
            return AIMessage(content=f'FAKE: created "{title}"')
        return AIMessage(content=f'FAKE: {text}')
