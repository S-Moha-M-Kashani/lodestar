"""The lab's chat-model access, built on the production seam.

CLAUDE.md's rule is that the lab tracks production seams, so the lab builds its
chat model with the brain's own make_chat_model. The payoff is that there is
exactly one LLM path in the repository, and whatever wins an experiment here
ports over unchanged.

One constraint to know about: `npm run raglab` runs with `--with
'langchain-openai<1'`, because ragas 0.4 requires it. uv resolves that overlay
by downgrading langchain-core to 0.3.x, and everything used here — BaseChatModel
and .invoke() — exists in both majors, so the lab is unaffected. Reaching for
langchain 1.x-only API in the lab (create_agent, the middleware types) would
break under that pin while passing the unit tests, which run on the project's
own environment.
"""
from langchain_core.messages import BaseMessage

from lodestar_brain.config import Settings
from lodestar_brain.llm.factory import make_chat_model

from .config import LabSettings


def lab_llm(settings: LabSettings):
    """The production chat model, or the offline fake when there is no key — the
    lab must remain runnable with no network at all."""
    return make_chat_model(Settings(
        llm_provider='openrouter' if settings.openrouter_api_key else 'fake',
        model=settings.llm_model,
        openrouter_api_key=settings.openrouter_api_key,
        openrouter_base_url=settings.openrouter_base_url))


def lab_chat(llm, messages: list[dict], model: str = '') -> BaseMessage:
    """Every LLM-backed lab step calls the model through here.

    An empty `model` means "whatever the client was built with", which is the
    lab's own convention for per-role model settings (see models.ROLES); a
    non-empty one is forwarded per request, so one client still serves every
    role without being rebuilt. Passing model='' through to invoke() would put
    a null model in the request instead, which is why this is a branch and not
    a default argument.
    """
    return llm.invoke(messages, model=model) if model else llm.invoke(messages)
