"""Deterministic, offline AI provider.

MOCKED: used for local development, automated tests and demos without an API key. Each task
registers a handler that derives output only from the typed ``context`` (never invents
facts that are not in it). Outputs are clearly labelled as produced by the mock provider
wherever they are stored (``ai_provider = "mock"``).

Tests can also inject failures (``fail_next``) to prove AI outages degrade gracefully.
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.ai.provider import AIError, AIInvalidOutputError, AIResult, AITask, AIUsage

MockHandler = Callable[[BaseModel | None], BaseModel]

_handlers: dict[AITask, MockHandler] = {}


def register_mock(task: AITask) -> Callable[[MockHandler], MockHandler]:
    def decorator(fn: MockHandler) -> MockHandler:
        _handlers[task] = fn
        return fn

    return decorator


class MockAIProvider:
    name = "mock"

    def __init__(self) -> None:
        self._failures: list[AIError] = []
        self.calls: list[AITask] = []

    def fail_next(self, error: AIError, times: int = 1) -> None:
        self._failures.extend([error] * times)

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
    ) -> AIResult[T]:
        self.calls.append(task)
        if self._failures:
            raise self._failures.pop(0)
        handler = _handlers.get(task)
        if handler is None:
            raise AIInvalidOutputError(f"no mock handler for {task}")
        raw: Any = handler(context)
        output = schema.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        usage = AIUsage(
            provider=self.name,
            model="mock-deterministic",
            input_tokens=len(prompt) // 4,
            output_tokens=len(output.model_dump_json()) // 4,
        )
        return AIResult(output=output, usage=usage)
