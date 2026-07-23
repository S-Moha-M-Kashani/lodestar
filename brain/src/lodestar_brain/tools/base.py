from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments object
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description,
            'parameters': self.parameters}}

    def run(self, arguments: dict) -> Any:
        return self.fn(**arguments)
