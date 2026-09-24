"""AIGateway: the single entry point application code uses to call an AI provider.

Responsibilities (cost + reliability controls):
- per-task timeouts (hard ``asyncio.wait_for`` on top of the SDK timeout)
- per-company daily token budget (fail closed with AIBudgetExceededError)
- usage/cost records for every attempt, success or failure
- converting every provider failure into an ``AIError`` so callers can degrade gracefully

No retries here: the SDK does bounded retries; callers decide whether a later retry makes
sense (e.g. the jobs system retries post-call processing a bounded number of times).
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select

from app.ai.models import AIUsageRecord
from app.ai.provider import (
    AIBudgetExceededError,
    AIError,
    AIProvider,
    AIResult,
    AITask,
    AITimeoutError,
    AIUsage,
)
from app.common.config import Settings, get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context

logger = logging.getLogger(__name__)

# Approximate USD per million tokens (input, output) for cost *estimates* only.
PRICING_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal(5), Decimal(25)),
    "claude-sonnet-5": (Decimal(2), Decimal(10)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
}

REALTIME_TASKS = {AITask.COPILOT, AITask.KNOWLEDGE_ANSWER}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    price = PRICING_PER_MTOK.get(model)
    if price is None:
        return Decimal(0)
    return (price[0] * input_tokens + price[1] * output_tokens) / Decimal(1_000_000)


class AIGateway:
    def __init__(self, provider: AIProvider, settings: Settings | None = None) -> None:
        self.provider = provider
        self.settings = settings or get_settings()

    def _timeout(self, task: AITask) -> float:
        if task in REALTIME_TASKS:
            return self.settings.ai_realtime_timeout_seconds
        return self.settings.ai_timeout_seconds

    async def tokens_used_today(self, company_id: uuid.UUID) -> int:
        since = datetime.now(UTC) - timedelta(days=1)
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=company_id))
            total = await session.scalar(
                select(
                    func.coalesce(
                        func.sum(AIUsageRecord.input_tokens + AIUsageRecord.output_tokens), 0
                    )
                ).where(
                    AIUsageRecord.company_id == company_id,
                    AIUsageRecord.created_at >= since,
                )
            )
        return int(total or 0)

    async def run[T: BaseModel](
        self,
        *,
        company_id: uuid.UUID,
        task: AITask,
        system: str,
        prompt: str,
        schema: type[T],
        context: BaseModel | None = None,
        max_tokens: int = 4000,
        call_id: uuid.UUID | None = None,
    ) -> AIResult[T]:
        budget = self.settings.ai_daily_token_budget
        if budget and await self.tokens_used_today(company_id) >= budget:
            await self._record(company_id, call_id, task, None, 0, "ai_budget_exceeded")
            raise AIBudgetExceededError("daily AI token budget reached for this company")

        timeout = self._timeout(task)
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self.provider.generate(
                    task=task,
                    system=system,
                    prompt=prompt,
                    schema=schema,
                    context=context,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout,
                ),
                timeout=timeout + 1,
            )
        except TimeoutError as exc:
            await self._record(company_id, call_id, task, None, _ms(start), AITimeoutError.code)
            raise AITimeoutError(f"{task} timed out") from exc
        except AIError as exc:
            await self._record(company_id, call_id, task, None, _ms(start), exc.code)
            logger.warning("ai_call_failed", extra={"task": task, "error": exc.code})
            raise
        except Exception as exc:  # any unexpected provider bug must not crash callers
            await self._record(company_id, call_id, task, None, _ms(start), "ai_error")
            logger.exception("ai_call_crashed", extra={"task": task})
            raise AIError("unexpected AI provider failure") from exc

        await self._record(company_id, call_id, task, result.usage, _ms(start), None)
        return result

    async def _record(
        self,
        company_id: uuid.UUID,
        call_id: uuid.UUID | None,
        task: AITask | str,
        usage: AIUsage | None,
        latency_ms: int,
        error_code: str | None,
    ) -> None:
        """Written in its own transaction so usage is kept even if the caller rolls back."""
        try:
            async with get_session_factory()() as session:
                await set_tenant_context(session, TenantContext(company_id=company_id))
                session.add(
                    AIUsageRecord(
                        company_id=company_id,
                        call_id=call_id,
                        task=str(task),
                        provider=usage.provider if usage else self.provider.name,
                        model=usage.model if usage else "",
                        input_tokens=usage.input_tokens if usage else 0,
                        output_tokens=usage.output_tokens if usage else 0,
                        latency_ms=latency_ms,
                        estimated_cost_usd=float(
                            estimate_cost(usage.model, usage.input_tokens, usage.output_tokens)
                        )
                        if usage
                        else 0,
                        success=error_code is None,
                        error_code=error_code,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("ai_usage_record_failed")


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


_gateway: AIGateway | None = None


def build_provider(settings: Settings) -> AIProvider:
    if settings.ai_provider == "anthropic":
        from app.ai.anthropic_provider import AnthropicProvider

        if settings.anthropic_api_key is None:
            raise RuntimeError("AI_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        realtime = settings.ai_model_realtime
        models = {
            task: realtime if task in REALTIME_TASKS else settings.ai_model for task in AITask
        }
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), models)
    from app.ai.mock_provider import MockAIProvider

    return MockAIProvider()


def get_ai_gateway() -> AIGateway:
    global _gateway
    if _gateway is None:
        settings = get_settings()
        _gateway = AIGateway(build_provider(settings), settings)
    return _gateway


def set_ai_provider(provider: AIProvider) -> AIGateway:
    """Test hook: swap the provider (e.g. a failing mock)."""
    global _gateway
    _gateway = AIGateway(provider, get_settings())
    return _gateway
