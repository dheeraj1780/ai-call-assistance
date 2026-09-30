"""Stale-call recovery: a periodic, restart-safe sweep for calls that can no longer finish on
their own (provider callback never came, the API restarted mid-call, a crash between committing
INITIATED and getting the provider's call id, ...).

The sweep keeps NO state in memory: it reads the database (a dedicated read-only RLS policy lets
it see in-flight calls across tenants, only inside a transaction declaring
``app.system_task = 'call_recovery'``) and then acts on each call under its own tenant's normal
context through the same ``force_complete`` / ``_finalize`` paths the End action uses. Every
step is idempotent, so overlapping sweeps or a sweep racing a provider webhook are harmless.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from app.calls.models import Call, CallStatus
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.jobs.service import register_job, register_periodic
from app.telephony import service

logger = logging.getLogger(__name__)

TERMINAL = ("COMPLETED", "NO_ANSWER", "CANCELLED", "FAILED")
LIVE = ("INITIATED", "RINGING", "CONNECTED", "ACTIVE", "ENDING")
# A finished call whose end-of-call processing has not been recorded for this long is re-run.
FINALIZE_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class StaleCall:
    id: uuid.UUID
    company_id: uuid.UUID
    status: CallStatus
    action: str  # fail_start | fail_connecting | end_timeout | end_inactive | end_max | finalize
    reason: str | None


def classify(row: Any, now: datetime) -> StaleCall | None:
    """Decide what (if anything) a call in this state needs. Pure function of the row + clock."""
    s = get_settings()
    status = CallStatus(row.status)
    # updated_at moves with every lifecycle change; last_activity_at with media frames/events.
    activity = max(t for t in (row.last_activity_at, row.updated_at) if t is not None)

    def stale(action: str, reason: str | None) -> StaleCall:
        return StaleCall(row.id, row.company_id, status, action, reason)

    if status.value in TERMINAL:
        ended = row.ended_at or row.updated_at
        if row.finalized_at is None and now - ended > FINALIZE_AFTER:
            return stale("finalize", None)
        return None
    if status == CallStatus.ENDING:
        requested = row.end_requested_at or row.updated_at
        if now - requested > timedelta(seconds=s.call_end_grace_seconds + 5):
            return stale("end_timeout", None)
        return None
    if status == CallStatus.INITIATED and not row.provider_call_id:
        if now - row.updated_at > timedelta(seconds=s.call_start_stale_seconds):
            return stale("fail_start", "start_interrupted")
        return None
    if status in (CallStatus.INITIATED, CallStatus.RINGING):
        if now - activity > timedelta(seconds=s.call_connecting_timeout_seconds):
            return stale("fail_connecting", "connect_timeout")
        return None
    # CONNECTED / ACTIVE
    started = row.started_at or row.created_at
    if now - started > timedelta(hours=s.call_max_duration_hours):
        return stale("end_max", "max_duration")
    if now - activity > timedelta(minutes=s.call_inactivity_timeout_minutes):
        return stale("end_inactive", "inactivity_timeout")
    return None


async def _find_candidates(now: datetime) -> list[StaleCall]:
    async with get_session_factory()() as session:
        await session.execute(text("SELECT set_config('app.system_task', 'call_recovery', true)"))
        rows = (
            await session.execute(
                select(
                    Call.id,
                    Call.company_id,
                    Call.status,
                    Call.provider_call_id,
                    Call.started_at,
                    Call.ended_at,
                    Call.end_requested_at,
                    Call.last_activity_at,
                    Call.finalized_at,
                    Call.created_at,
                    Call.updated_at,
                ).where(
                    Call.status.in_(LIVE)
                    | (
                        Call.status.in_(TERMINAL)
                        & Call.finalized_at.is_(None)
                        & Call.provider.is_not(None)  # hand-logged calls have nothing to finalise
                        & (Call.updated_at < now - FINALIZE_AFTER)
                    )
                )
            )
        ).all()
    found = (classify(r, now) for r in rows)
    return [c for c in found if c is not None]


async def _hang_up(company_id: uuid.UUID, call_id: uuid.UUID) -> None:
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        call = await session.scalar(
            select(Call).where(Call.company_id == company_id, Call.id == call_id)
        )
        if call is not None:
            await service.hang_up_provider(session, company_id, call)


async def recover_calls(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for call in await _find_candidates(now):
        try:
            if call.action == "finalize":
                # The server stopped between "call ended" and "end-of-call processing done".
                service.schedule_finalize(call.company_id, call.id)
                await service.wait_finalized(call.id)
            else:
                if call.action != "fail_start":
                    await _hang_up(call.company_id, call.id)  # best effort: no orphan provider call
                await service.force_complete(call.company_id, call.id, error=call.reason)
        except Exception:
            logger.exception("call_recovery_failed", extra={"call_id": str(call.id)})
            continue
        counts[call.action] = counts.get(call.action, 0) + 1
        logger.warning(
            "call_recovered",
            extra={"call_id": str(call.id), "action": call.action, "was": call.status.value},
        )
    return counts


@register_periodic("calls.recover", interval_seconds=60)
@register_job("calls.recover")
async def _recover_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    await recover_calls()
