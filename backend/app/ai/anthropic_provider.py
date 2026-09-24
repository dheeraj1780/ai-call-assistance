"""Claude (Anthropic) implementation of AIProvider.

Uses ``AsyncAnthropic().messages.parse(output_format=<Pydantic model>)`` so every response is
validated against our schema by the SDK. The only module that imports the vendor SDK.

Status: IMPLEMENTED, NOT LIVE VERIFIED (no API key was available during development). All
behaviour around it (timeouts, refusals, invalid output, budgets) is tested with fakes.
"""

import logging

import anthropic
from pydantic import BaseModel, ValidationError

from app.ai.provider import (
    AIInvalidOutputError,
    AIRefusalError,
    AIResult,
    AITask,
    AITimeoutError,
    AIUnavailableError,
    AIUsage,
)

logger = logging.getLogger(__name__)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, models: dict[AITask, str], max_retries: int = 1) -> None:
        # Retries are few on purpose: live-call tasks are latency bound, and the gateway
        # enforces an overall timeout anyway.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=max_retries)
        self._models = models

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
        model = self._models[task]
        try:
            response = await self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                timeout=timeout_seconds,
            )
        except anthropic.APITimeoutError as exc:
            raise AITimeoutError(str(exc)) from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise AIUnavailableError(type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            logger.warning("anthropic_api_error", extra={"status": exc.status_code, "task": task})
            raise AIUnavailableError(f"status {exc.status_code}") from exc
        except ValidationError as exc:
            raise AIInvalidOutputError("schema validation failed") from exc

        usage = AIUsage(
            provider=self.name,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            raise AIRefusalError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise AIInvalidOutputError("output truncated at max_tokens")
        parsed = response.parsed_output
        if parsed is None:
            raise AIInvalidOutputError("no structured output returned")
        return AIResult(output=parsed, usage=usage)
