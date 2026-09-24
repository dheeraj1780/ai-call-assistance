"""Provider-neutral AI interface.

Domain code depends only on this module and its own Pydantic schemas - never on a vendor
SDK. A provider receives:

- ``system``/``prompt``: text for real LLMs. Untrusted content (transcripts, documents,
  customer data) is always wrapped in delimited data sections by the caller
  (see ``app/ai/safety.py``) and the system prompt says to treat it as data.
- ``context``: the same information as a typed object, used by the deterministic mock
  provider so local/offline runs produce sensible, clearly-labelled output.
- ``schema``: the Pydantic model the output must validate against. Providers never return
  unvalidated model text.
"""

import enum
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class AITask(enum.StrEnum):
    AGENDA = "AGENDA"
    COPILOT = "COPILOT"
    KNOWLEDGE_ANSWER = "KNOWLEDGE_ANSWER"
    POST_CALL = "POST_CALL"
    FOLLOW_UP = "FOLLOW_UP"


class AIError(Exception):
    """Base class. AI failures must degrade features, never break calls or requests."""

    code = "ai_error"


class AIUnavailableError(AIError):
    code = "ai_unavailable"


class AITimeoutError(AIError):
    code = "ai_timeout"


class AIRefusalError(AIError):
    code = "ai_refusal"


class AIInvalidOutputError(AIError):
    code = "ai_invalid_output"


class AIBudgetExceededError(AIError):
    code = "ai_budget_exceeded"


@dataclass(frozen=True, slots=True)
class AIUsage:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AIResult[T: BaseModel]:
    output: T
    usage: AIUsage


class AIProvider(Protocol):
    name: str

    async def generate[T: BaseModel](
        self,
        *,
        task: AITask,
        system: str,
        prompt: str,
        schema: type[T],
        context: BaseModel | None,
        max_tokens: int,
        timeout_seconds: float,
    ) -> AIResult[T]: ...
