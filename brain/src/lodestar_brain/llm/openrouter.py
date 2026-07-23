import json

import httpx

from .base import AssistantTurn, ToolCall


class OpenRouterProvider:
    def __init__(self, api_key: str, base_url: str, default_model: str,
                 timeout: float = 90.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.default_model = default_model
        self.timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             model: str | None = None) -> AssistantTurn:
        payload: dict = {'model': model or self.default_model, 'messages': messages}
        if tools:
            payload['tools'] = tools
        res = httpx.post(f'{self.base_url}/chat/completions',
                         headers={'Authorization': f'Bearer {self.api_key}'},
                         json=payload, timeout=self.timeout)
        res.raise_for_status()
        message = res.json()['choices'][0]['message']
        calls = [ToolCall(id=c['id'], name=c['function']['name'],
                          arguments=json.loads(c['function']['arguments'] or '{}'))
                 for c in message.get('tool_calls') or []]
        return AssistantTurn(content=message.get('content'), tool_calls=calls)
