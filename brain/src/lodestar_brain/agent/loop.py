"""Minimal function-calling agent loop over any LLMProvider. Deliberately
framework-free: the loop is ~60 lines and the seams (LLMProvider, Tool) are
protocols, so swapping in LangGraph/another framework later means replacing
this one file."""
import json
from dataclasses import dataclass, field

from ..llm.base import LLMProvider
from ..tools.base import Tool

SYSTEM_PROMPT = """You are Lodestar's assistant — a research companion and coach \
for a personal question board ("your compass for open questions").

You can: research and draft answers (web_search + find_related, cite urls), \
operate the board (list/create/update questions), break fuzzy questions into \
concrete sub-questions, and surface connections (find_related returns Leiden \
community ids — same community = same theme; point out likely duplicates).

Board columns: inbox, to-research, in-progress, answered. \
Priorities: high, medium, low. Importance/urgency: high, low, or empty.

Rules: never invent question ids — look them up with list_questions or \
find_related first. When you change the board, say exactly what you changed. \
When research produces an answer, offer to save it into the question's notes. \
Keep replies short and concrete."""


@dataclass
class AgentStep:
    tool: str
    arguments: dict
    result: object


@dataclass
class AgentResult:
    reply: str
    steps: list[AgentStep] = field(default_factory=list)


class Agent:
    def __init__(self, llm: LLMProvider, tools: list[Tool],
                 system_prompt: str = SYSTEM_PROMPT, max_steps: int = 8):
        self.llm = llm
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = system_prompt
        self.max_steps = max_steps

    def run(self, messages: list[dict], model: str | None = None) -> AgentResult:
        convo = [{'role': 'system', 'content': self.system_prompt}, *messages]
        specs = [tool.spec() for tool in self.tools.values()]
        steps: list[AgentStep] = []
        for _ in range(self.max_steps):
            turn = self.llm.chat(convo, tools=specs, model=model)
            if not turn.tool_calls:
                return AgentResult(reply=turn.content or '', steps=steps)
            convo.append({'role': 'assistant', 'content': turn.content,
                          'tool_calls': [{'id': c.id, 'type': 'function',
                                          'function': {'name': c.name,
                                                       'arguments': json.dumps(c.arguments)}}
                                         for c in turn.tool_calls]})
            for call in turn.tool_calls:
                if len(steps) >= self.max_steps:
                    break
                tool = self.tools.get(call.name)
                if tool is None:
                    result: object = {'error': f'unknown tool {call.name!r}'}
                else:
                    try:
                        result = tool.run(call.arguments)
                    except Exception as exc:
                        result = {'error': str(exc)}
                steps.append(AgentStep(tool=call.name, arguments=call.arguments,
                                       result=result))
                convo.append({'role': 'tool', 'tool_call_id': call.id,
                              'content': json.dumps(result, default=str)})
        return AgentResult(
            reply='I hit my step limit before finishing — try a smaller request.',
            steps=steps)
