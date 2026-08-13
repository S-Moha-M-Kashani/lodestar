"""The agent: its prompt, its loop, and the result the rest of the brain sees.

One module held four unrelated things — the text the model is given, the loop
that runs it, the reading of a transcript into our own types, and a tool-error
handler that is middleware and belongs with the rest of the middleware. The
public surface is unchanged: `from lodestar_brain.agent import LodestarAgent,
AgentResult, SYSTEM_PROMPT, …` resolves exactly as it did.

| Module | Job |
| --- | --- |
| `prompt.py` | the system prompt, and the fence rule appended to it |
| `result.py` | `AgentResult`/`AgentStep`, and the readers that build them |
| `state.py` | `LodestarState` (checkpointed) and `TurnContext` (per request) |
| `graph.py` | `LodestarAgent` — `create_agent`, the graph cache, run/arun/astream |

**The direction is one-way.** `prompt`, `result` and `state` import nothing from
this package, `graph` imports all three, and nothing here imports `__init__` —
so the module everything needs is the one that needs nothing. The fence itself
is not here at all: it is `middleware/untrusted.py`, next to the error handler it
has to sit outside of.
"""
from .graph import LodestarAgent
from .prompt import SYSTEM_PROMPT
from .result import (STEP_LIMIT_REPLY, AgentResult, AgentStep, _calls_in,
                     _reply_from, _result_from, _steps_from, _text,
                     _usage_from)
from .state import LodestarState, TurnContext

__all__ = ['STEP_LIMIT_REPLY', 'SYSTEM_PROMPT', 'AgentResult', 'AgentStep',
           'LodestarAgent', 'LodestarState', 'TurnContext']
