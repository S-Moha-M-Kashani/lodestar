"""Provider-agnostic LLM contract. Messages/tools use the OpenAI wire format,
so any OpenAI-compatible backend (OpenRouter, Ollama, vLLM, ...) is a drop-in."""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class AssistantTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None) -> AssistantTurn: ...
