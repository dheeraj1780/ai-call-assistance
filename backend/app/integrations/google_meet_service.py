"""Google Meet: per-salesperson Google sign-in, meeting lookup, and attaching the live copilot to
an active conference through the Meet Media API.

Security / privacy:
- OAuth ``state`` is signed, short-lived and carries (user, company); membership is re-checked.
- The refresh token is encrypted at rest (TOKEN_ENCRYPTION_KEY). Access tokens are minted per
  request, never stored, never logged and never sent to the browser.
- The browser only exchanges SDP with our API; WebRTC media flows browser <-> Google, and the
  browser forwards mixed PCM to the existing media WebSocket (per-call HMAC token). No audio is
  stored anywhere.
- Every connect / disconnect / media attach is audited (no tokens, no meeting content).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.calls.models import TERMINAL_CALL_STATUSES, Call, CallChannel, CallStatus
from app.calls.service import get_call
from app.common.config import get_settings
from app.common.crypto import decrypt, encrypt, sign_state, verify_state
from app.common.db import TenantContext, set_tenant_context
from app.common.errors import AppError, ForbiddenError, InvalidStateError
from app.integrations import service as integrations
from app.integrations.domain import Capability, IntegrationMode, Provider
from app.integrations.models import Integration, IntegrationUserConnection
from app.integrations.providers import factory
from app.integrations.providers import google_meet as meet
from app.integrations.providers.base import ConnectionReport, ProviderAuthError, ProviderError
from app.telephony.provider import ProviderCallState, TelephonyEvent
from app.tenants.membership import ensure_member

logger = logging.getLogger(__name__)
STATE_PURPOSE = "google-meet-oauth"
PROVIDER_NAMES = ("google-meet", "google-meet-mock")


class GoogleMeetNotConnectedError(AppError):
    status_code = 409
    code = "google_meet_not_connected"
    message = "Connect your Google account for Meet first (Settings > Integrations)."


def as_app_error(exc: meet.MeetError) -> AppError:
    return AppError(
        exc.message, code=exc.code, details={"trace_id": exc.trace_id, "retryable": exc.retryable}
    ).with_status(exc.status_code)


def redirect_uri() -> str:
    s = get_settings()
    return s.google_meet_redirect_uri or (
        f"{s.public_base_url.rstrip('/')}/api/v1/integrations/google-meet/oauth/callback"
    )


async def _record(session: AsyncSession, company_id: uuid.UUID) -> Integration:
    record = await integrations.get_record(session, company_id, Provider.GOOGLE_MEET)
    if record is None:
        raise integrations.IntegrationNotReadyError(
            "Google Meet is not configured. An admin can set it up in Settings > Integrations."
        )
    return record


# ---- per-user OAuth ----------------------------------------------------------------------------


async def start_connect(session: AsyncSession, principal: Principal) -> str:
    record = await _record(session, principal.company_id)
    if record.mode == IntegrationMode.MOCK:
        raise AppError("Mock mode needs no Google sign-in.", code="mock_mode").with_status(409)
    try:
        oauth = factory.google_meet_oauth_client()
    except factory.CredentialsMissingError as exc:
        raise integrations.IntegrationNotReadyError(
            f"Google Meet OAuth is not configured on the server ({exc})."
        ) from None
    state = sign_state(
        {"u": str(principal.user_id), "c": str(principal.company_id), "n": uuid.uuid4().hex},
        purpose=STATE_PURPOSE,
    )
    return oauth.authorization_url(state=state, redirect_uri=redirect_uri())


async def complete_connect(session: AsyncSession, *, code: str, state: str) -> None:
    data = verify_state(state, purpose=STATE_PURPOSE)
    user_id, company_id = uuid.UUID(data["u"]), uuid.UUID(data["c"])
    await set_tenant_context(session, TenantContext(company_id=company_id, user_id=user_id))
    await ensure_member(session, company_id, user_id, field="user")
    record = await _record(session, company_id)
    oauth = factory.google_meet_oauth_client()
    tokens = await oauth.exchange_code(code=code, redirect_uri=redirect_uri())
    if not tokens.refresh_token:
        raise AppError("Google did not grant offline access", code="MEET_NO_REFRESH_TOKEN")
    missing = meet.missing_scopes(tokens.scopes)
    if missing:
        # Google lets users untick scopes on the consent screen.
        raise AppError(
            "Google did not grant the required Meet permissions.",
            code="MEET_SCOPE_MISSING",
            details={"missing": missing},
        ).with_status(403)
    identity = await oauth.identity(tokens.access_token)
    conn = await session.scalar(
        select(IntegrationUserConnection).where(
            IntegrationUserConnection.company_id == company_id,
            IntegrationUserConnection.user_id == user_id,
            IntegrationUserConnection.provider == Provider.GOOGLE_MEET.value,
        )
    )
    if conn is None:
        conn = IntegrationUserConnection(
            company_id=company_id,
            user_id=user_id,
            provider=Provider.GOOGLE_MEET.value,
            integration_id=record.id,
        )
        session.add(conn)
    conn.integration_id = record.id
    conn.external_user_id = identity.subject[:128] or None
    conn.account_email = identity.email
    conn.encrypted_refresh_token = encrypt(tokens.refresh_token)
    conn.scopes = " ".join(sorted(tokens.scopes))[:1000]
    conn.status = "ACTIVE"
    conn.last_error = None
    audit.record(
        session,
        "google_meet.user_connected",
        company_id=company_id,
        actor_user_id=user_id,
        entity_type="integration_user_connection",
        details={"scopes": sorted(tokens.scopes & set(meet.REQUIRED_SCOPES))},
    )
    await session.commit()


async def get_connection(
    session: AsyncSession, principal: Principal
) -> IntegrationUserConnection | None:
    conn: IntegrationUserConnection | None = await session.scalar(
        select(IntegrationUserConnection).where(
            IntegrationUserConnection.company_id == principal.company_id,
            IntegrationUserConnection.user_id == principal.user_id,
            IntegrationUserConnection.provider == Provider.GOOGLE_MEET.value,
        )
    )
    return conn


async def disconnect_user(session: AsyncSession, principal: Principal) -> None:
    conn = await get_connection(session, principal)
    if conn is None:
        return
    try:
        # Revoke at Google as well (best effort): the grant should not outlive the connection.
        await factory.google_meet_oauth_client().revoke(decrypt(conn.encrypted_refresh_token))
    except (ProviderError, factory.CredentialsMissingError, ValueError):
        logger.warning("google_meet_revoke_failed")
    await session.delete(conn)
    audit.record(
        session,
        "google_meet.user_disconnected",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration_user_connection",
    )
    await session.commit()


async def _access_token(session: AsyncSession, principal: Principal) -> tuple[str, frozenset[str]]:
    conn = await get_connection(session, principal)
    if conn is None or conn.status != "ACTIVE":
        raise GoogleMeetNotConnectedError()
    oauth = factory.google_meet_oauth_client()
    try:
        tokens = await oauth.refresh(decrypt(conn.encrypted_refresh_token))
    except (ProviderAuthError, ValueError):
        conn.status = "REAUTH_REQUIRED"
        conn.last_error = "reauth_required"
        await session.commit()
        raise GoogleMeetNotConnectedError(
            "Your Google sign-in expired or was revoked. Reconnect Google Meet.",
            code="MEET_AUTH_EXPIRED",
        ) from None
    if tokens.refresh_token:
        conn.encrypted_refresh_token = encrypt(tokens.refresh_token)
        await session.commit()
    return tokens.access_token, tokens.scopes


# ---- connection test ---------------------------------------------------------------------------


async def test_connection(
    session: AsyncSession, principal: Principal, record: Integration
) -> ConnectionReport:
    """What can be verified without a live conference: OAuth client, the tester's Google
    connection, token refresh and granted scopes. Developer Preview enrolment can only be proven
    by a real connectActiveConference (reported separately)."""
    if record.mode == IntegrationMode.MOCK:
        return meet.mock_test_report()
    report = ConnectionReport()
    try:
        factory.google_meet_oauth_client()
        report.add("oauth_client", "Google OAuth client configured on the server", True)
    except factory.CredentialsMissingError as exc:
        report.add(
            "oauth_client", "Google OAuth client configured on the server", False, f"Missing: {exc}"
        )
        return report
    conn = await get_connection(session, principal)
    if not report.add(
        "user_connection",
        "Your Google account is connected",
        conn is not None,
        None if conn else "Connect your Google account first.",
    ):
        return report
    try:
        _, scopes = await _access_token(session, principal)
    except GoogleMeetNotConnectedError as exc:
        report.add("token", "Google sign-in is valid", False, exc.message)
        return report
    report.add("token", "Google sign-in is valid", True)
    missing = meet.missing_scopes(scopes)
    report.missing_permissions = missing
    report.add(
        "scopes",
        "Meet permissions granted (space read, meeting audio)",
        not missing,
        ", ".join(missing) or None,
    )
    report.add(
        "developer_preview",
        "Meet Media API Developer Preview access",
        bool(record.config.get("media_verified_at")),
        "Confirmed only when a live meeting connection succeeds.",
        required=False,
    )
    report.account_label = conn.account_email if conn else None
    report.capabilities = {
        Capability.MEETING_LOOKUP.value: not missing,
        Capability.REAL_TIME_CALL.value: not missing,
    }
    return report


# ---- meeting lookup ----------------------------------------------------------------------------


async def lookup_meeting(
    session: AsyncSession, principal: Principal, link_or_code: str
) -> meet.MeetSpace:
    code = meet.parse_meeting_code(link_or_code)
    if code is None:
        raise AppError(
            "Not a Google Meet link or meeting code.", code="MEET_INVALID_LINK"
        ).with_status(422)
    record = await integrations.require_capability(
        session, principal.company_id, Provider.GOOGLE_MEET, Capability.MEETING_LOOKUP
    )
    api = factory.google_meet_api(record)
    token = (
        "mock"
        if record.mode == IntegrationMode.MOCK
        else (await _access_token(session, principal))[0]
    )
    try:
        return await api.get_space(token, code)
    except meet.MeetError as exc:
        raise as_app_error(exc) from None


# ---- attach the copilot to a call's conference -------------------------------------------------


@dataclass(frozen=True)
class ConnectResult:
    answer: str
    trace_id: str | None
    space: str
    media_ws_path: str
    mock: bool


def _media_ws_path(provider_name: str, call_id: uuid.UUID) -> str:
    from app.telephony import service as telephony

    _, media_url = telephony._callback_urls(provider_name, call_id)
    path = media_url.split("://", 1)[1]
    return path[path.index("/") :]


async def _meet_call(session: AsyncSession, principal: Principal, call_id: uuid.UUID) -> Call:
    call = await get_call(session, principal, call_id)
    if not (principal.is_admin or call.user_id in (None, principal.user_id)):
        raise ForbiddenError("Only the assigned user or an admin can attach the copilot")
    if call.channel != CallChannel.GOOGLE_MEET or call.provider not in PROVIDER_NAMES:
        raise InvalidStateError("This is not a started Google Meet call")
    return call


async def connect_call(
    session: AsyncSession, principal: Principal, call_id: uuid.UUID, offer: str
) -> ConnectResult:
    call = await _meet_call(session, principal, call_id)
    if CallStatus(call.status) in TERMINAL_CALL_STATUSES or call.status == CallStatus.PLANNED:
        raise InvalidStateError("Start the call first; it must be in progress")
    code = meet.meeting_code_of(call.provider_call_id)
    if code is None:
        raise InvalidStateError("This call has no Google Meet meeting")
    record = await _record(session, principal.company_id)
    api = factory.google_meet_api(record)
    mock = record.mode == IntegrationMode.MOCK
    try:
        meet.validate_offer(offer)
        token = "mock" if mock else (await _access_token(session, principal))[0]
        space = await api.get_space(token, code)
        if not space.active_conference:
            raise meet.MeetError(
                *meet.REASONS["NO_ACTIVE_CONFERENCE"][:2], status_code=409, retryable=True
            )
        answer = await api.connect_active_conference(token, space.name, offer)
    except meet.MeetError as exc:
        audit.record(
            session,
            "google_meet.media_connect_failed",
            company_id=principal.company_id,
            actor_user_id=principal.user_id,
            entity_type="call",
            entity_id=call.id,
            details={"code": exc.code},
        )
        await session.commit()
        logger.warning(
            "google_meet_connect_failed",
            extra={"call_id": str(call.id), "code": exc.code, "trace_id": exc.trace_id},
        )
        raise as_app_error(exc) from None
    if not mock and not record.config.get("media_verified_at"):
        # First real connectActiveConference success: Google accepted the project, the OAuth
        # principal and the participants for the Media API (Developer Preview).
        record.config = {**record.config, "media_verified_at": datetime.now(UTC).isoformat()}
    audit.record(
        session,
        "google_meet.media_connected",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="call",
        entity_id=call.id,
        details={"mock": mock},
    )
    await session.commit()
    logger.info(
        "google_meet_connected", extra={"call_id": str(call.id), "trace_id": answer.trace_id}
    )
    return ConnectResult(
        answer=answer.answer,
        trace_id=answer.trace_id,
        space=space.name,
        media_ws_path=_media_ws_path(call.provider or "google-meet", call.id),
        mock=mock,
    )


# ---- session state reported by the browser client -----------------------------------------------

ClientState = Literal["waiting", "joined", "disconnected", "failed"]


async def report_event(
    session: AsyncSession,
    principal: Principal,
    call_id: uuid.UUID,
    *,
    event_id: str,
    state: ClientState,
    reason: str | None,
) -> str:
    """The Meet Media API session status (session-control channel) as seen by the browser.
    Idempotent per (call, event_id). Ending the copilot session ends the call."""
    from app.telephony import service as telephony

    call = await _meet_call(session, principal, call_id)
    if state == "waiting":
        mapped = ProviderCallState.CONNECTED
    elif state == "joined":
        mapped = ProviderCallState.ACTIVE
    elif state == "disconnected" and call.status in (CallStatus.CONNECTED, CallStatus.ACTIVE):
        mapped = ProviderCallState.COMPLETED
    else:
        mapped = ProviderCallState.FAILED
    return await telephony.process_event_for_call(
        call.provider or "google-meet",
        principal.company_id,
        call.id,
        TelephonyEvent(
            event_id=f"{call.id}:{event_id}"[:128],
            provider_call_id=call.provider_call_id or "",
            state=mapped,
            occurred_at=datetime.now(UTC),
            error_code=reason[:64] if reason and mapped == ProviderCallState.FAILED else None,
        ),
    )
