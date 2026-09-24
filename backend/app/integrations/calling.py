"""Which calling provider handles a call (per company and channel).

PHONE: the company's Plivo integration when PHONE_CALL is enabled (LIVE -> Plivo, MOCK -> the
mock telephony provider); otherwise the server fallback TELEPHONY_PROVIDER=mock (development).
TEAMS: the company's Teams integration when REAL_TIME_CALL is enabled (LIVE -> media gateway,
MOCK -> in-process mock gateway + simulator).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.calls.models import Call, CallChannel, TranscriptPersistence
from app.common.config import get_settings
from app.integrations import service as integrations
from app.integrations.domain import Capability, Provider
from app.integrations.providers import factory
from app.telephony.provider import TelephonyProvider, get_telephony_provider


@dataclass(frozen=True)
class ResolvedCalling:
    provider: TelephonyProvider
    integration_id: uuid.UUID | None
    persistence: TranscriptPersistence


async def resolve_for_start(
    session: AsyncSession, company_id: uuid.UUID, call: Call
) -> ResolvedCalling:
    if call.channel == CallChannel.TEAMS:
        record = await integrations.require_capability(
            session, company_id, Provider.MICROSOFT_TEAMS, Capability.REAL_TIME_CALL
        )
        persistence = (
            TranscriptPersistence.PENDING_RECORDING_STATUS
            if record.config.get("call_persistence") == "RECORDING_DECLARED"
            else TranscriptPersistence.TRANSIENT
        )
        try:
            provider = factory.teams_calling_provider(record)
        except factory.CredentialsMissingError as exc:
            raise integrations.IntegrationNotReadyError(
                f"Teams calling is missing: {exc}"
            ) from None
        return ResolvedCalling(provider, record.id, persistence)

    plivo = await integrations.capability_record(
        session, company_id, Provider.PLIVO, Capability.PHONE_CALL
    )
    if plivo is not None:
        stream = Capability.MEDIA_STREAM.value in plivo.enabled_capabilities
        try:
            phone: TelephonyProvider = factory.plivo_provider(
                plivo, integrations.secrets_of(plivo), stream_enabled=stream
            )
        except factory.CredentialsMissingError as exc:
            raise integrations.IntegrationNotReadyError(f"Plivo is missing: {exc}") from None
        return ResolvedCalling(phone, plivo.id, TranscriptPersistence.PERSISTED)
    if get_settings().telephony_provider == "mock":
        return ResolvedCalling(get_telephony_provider(), None, TranscriptPersistence.PERSISTED)
    raise integrations.IntegrationNotReadyError(
        "Phone calling is not set up. An admin can enable Plivo in Settings > Integrations."
    )


async def provider_for_call(
    session: AsyncSession, company_id: uuid.UUID, call: Call
) -> TelephonyProvider | None:
    """The provider that placed ``call`` (used to hang up), or None if it cannot be built."""
    name = call.provider or ""
    if name == "mock":
        return get_telephony_provider()
    if name == "plivo":
        record = await integrations.get_record(session, company_id, Provider.PLIVO)
        if record is None:
            return None
        try:
            return factory.plivo_provider(
                record, integrations.secrets_of(record), stream_enabled=False
            )
        except factory.CredentialsMissingError:
            return None
    if name in ("teams", "teams-mock"):
        record = await integrations.get_record(session, company_id, Provider.MICROSOFT_TEAMS)
        if record is None:
            return None
        try:
            return factory.teams_calling_provider(record)
        except factory.CredentialsMissingError:
            return None
    return None
