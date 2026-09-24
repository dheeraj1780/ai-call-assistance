"""Minimal Postgres-backed job queue (ADR-005 in the Phase 0 plan: no Redis/Celery).

- ``enqueue`` is idempotent per ``dedupe_key``.
- Workers claim with ``FOR UPDATE SKIP LOCKED``; a crashed worker's lock expires.
- Bounded retries with exponential backoff; then FAILED (never an infinite loop).
- Error details stored are the exception class only (no personal data).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.config import get_settings
from app.common.db import get_session_factory
from app.jobs.models import Job, JobStatus

logger = logging.getLogger(__name__)

JobHandler = Callable[[uuid.UUID | None, dict[str, Any]], Awaitable[None]]
_handlers: dict[str, JobHandler] = {}

LOCK_SECONDS = 300
BACKOFF_SECONDS = 30


def register_job(kind: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(fn: JobHandler) -> JobHandler:
        _handlers[kind] = fn
        return fn

    return decorator


async def enqueue(
    session: AsyncSession,
    kind: str,
    *,
    company_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
) -> None:
    """Add a job in the caller's transaction (it runs only if the caller commits)."""
    stmt = insert(Job).values(
        id=uuid.uuid4(),
        company_id=company_id,
        kind=kind,
        payload=payload or {},
        status=JobStatus.PENDING.value,
        attempts=0,
        max_attempts=max_attempts,
        run_after=run_after or datetime.now(UTC),
        dedupe_key=dedupe_key,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedupe_key"], index_where=text("dedupe_key IS NOT NULL")
        )
    await session.execute(stmt)


_CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'RUNNING', attempts = attempts + 1,
                    locked_until = now() + make_interval(secs => :lock), updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE (status = 'PENDING' AND run_after <= now())
           OR (status = 'RUNNING' AND locked_until < now())
        ORDER BY run_after
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, company_id, kind, payload, attempts, max_attempts
    """
)


async def run_one() -> bool:
    """Claim and run one due job. Returns False if there was nothing to do."""
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(_CLAIM_SQL, {"lock": LOCK_SECONDS})).first()
        await session.commit()
    if row is None:
        return False

    handler = _handlers.get(row.kind)
    error: str | None = None
    if handler is None:
        error = "UnknownJobKind"
    else:
        try:
            await handler(row.company_id, dict(row.payload))
        except Exception as exc:
            error = type(exc).__name__
            logger.exception("job_failed", extra={"job_kind": row.kind, "job_id": str(row.id)})

    async with factory() as session:
        job = await session.get(Job, row.id)
        if job is not None:
            if error is None:
                job.status = JobStatus.SUCCEEDED.value
                job.last_error = None
            elif job.attempts < job.max_attempts and error != "UnknownJobKind":
                job.status = JobStatus.PENDING.value
                job.run_after = datetime.now(UTC) + timedelta(
                    seconds=BACKOFF_SECONDS * 2 ** (job.attempts - 1)
                )
                job.last_error = error
            else:
                job.status = JobStatus.FAILED.value
                job.last_error = error
            job.locked_until = None
            await session.commit()
    return True


async def drain(max_jobs: int = 100) -> int:
    """Run due jobs until none are left (used by tests and admin tooling)."""
    count = 0
    while count < max_jobs and await run_one():
        count += 1
    return count


async def worker_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    last_sweep = 0.0
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            if (
                "retention.sweep" in _handlers
                and loop.time() - last_sweep >= settings.retention_sweep_interval_seconds
            ):
                last_sweep = loop.time()
                await schedule_retention_sweep()
            worked = await run_one()
        except Exception:
            logger.exception("job_worker_error")
            worked = False
        if not worked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.jobs_poll_interval_seconds)


async def schedule_retention_sweep() -> None:
    bucket = datetime.now(UTC).strftime("%Y%m%d%H")
    async with get_session_factory()() as session:
        await enqueue(session, "retention.sweep", company_id=None, dedupe_key=f"retention:{bucket}")
        await session.commit()
