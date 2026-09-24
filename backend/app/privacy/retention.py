"""Transcript retention (default 30 days, configurable per company).

The sweep runs as a background job (hourly by default) and deletes transcript segments and
live copilot insights whose ``expires_at`` has passed. These tables are RLS-protected; the only
cross-tenant access the sweep has is a dedicated policy that allows DELETE of *expired* rows
when the transaction sets ``app.system_task = 'retention'``.

Structured notes keep their text (they are business records) but lose the link to the
purged segment (FK ``SET NULL``). Raw audio is never stored, so there is nothing to purge.
Logs never contain transcript text. Knowledge embeddings are built from uploaded documents,
never from transcripts.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.common.db import get_session_factory
from app.jobs.service import register_job

logger = logging.getLogger(__name__)


async def purge_expired() -> dict[str, int]:
    async with get_session_factory()() as session:
        await session.execute(text("SELECT set_config('app.system_task', 'retention', true)"))
        insights = await session.execute(
            text("DELETE FROM copilot_insights WHERE expires_at < now()")
        )
        segments = await session.execute(
            text("DELETE FROM transcript_segments WHERE expires_at < now()")
        )
        await session.commit()
    result = {
        "transcript_segments": segments.rowcount or 0,  # type: ignore[attr-defined]
        "copilot_insights": insights.rowcount or 0,  # type: ignore[attr-defined]
    }
    logger.info("retention_sweep", extra=result)
    return result


@register_job("retention.sweep")
async def _retention_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    await purge_expired()
