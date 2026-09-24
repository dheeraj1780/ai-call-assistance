"""Teams messaging: per-salesperson Microsoft sign-in, chat linking, change-notification
subscriptions (created, renewed, deleted) and message retrieval.

Security:
- OAuth ``state`` is signed and short-lived and carries (user, company); membership is
  re-checked before anything is stored.
- Refresh tokens are encrypted at rest; access tokens are never stored or logged.
- Notifications are authenticated by a per-integration ``clientState`` (HMAC) and routed to the
  tenant through ``integration_routes`` - never by an id inside the notification.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.config import get_settings
from app.common.crypto import decrypt, encrypt, sign_state, verify_state
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import AppError, ConflictError, NotFoundError
from app.contacts.repository import ContactRepository
from app.conversations import service as conversations
from app.conversations.models import (
    CommunicationSession,
    ContactIdentity,
    IdentityKind,
    MatchStatus,
)
from app.integrations import service as integrations
from app.integrations.domain import Capability, Channel, IntegrationMode, Provider
from app.integrations.models import (
    Integration,
    IntegrationRoute,
    IntegrationSubscription,
    IntegrationUserConnection,
)
from app.integrations.providers import factory
from app.integrations.providers.base import MessageProvider, ProviderAuthError, ProviderError
from app.integrations.providers.teams import (
    MOCK_CHATS,
    GraphClient,
    MockTeamsMessageProvider,
    TeamsChat,
    TeamsMessageProvider,
    client_state_for,
    missing_messaging_scopes,
    normalize_message,
)
from app.jobs.service import register_job, register_periodic
from app.tenants.membership import ensure_member

logger = logging.getLogger(__name__)
STATE_PURPOSE = "teams-oauth"
CONSENT_PURPOSE = "teams-admin-consent"
RENEW_BEFORE = timedelta(minutes=25)


class TeamsNotConnectedError(AppError):
    status_code = 409
    code = "teams_not_connected"
    message = "Connect your Microsoft Teams account first (Settings > Integrations)."


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def redirect_uri() -> str:
    return f"{_base()}/api/v1/integrations/microsoft-teams/oauth/callback"


def consent_redirect_uri() -> str:
    return f"{_base()}/api/v1/integrations/microsoft-teams/admin-consent/callback"


def notification_url(integration_id: uuid.UUID) -> str:
    return f"{_base()}/api/v1/integrations/teams/webhooks/{integration_id}"


async def _live_record(session: AsyncSession, company_id: uuid.UUID) -> Integration:
    record = await integrations.get_record(session, company_id, Provider.MICROSOFT_TEAMS)
    if record is None:
        raise integrations.IntegrationNotReadyError(
            "Microsoft Teams is not configured. An admin can set it up in Settings > Integrations."
        )
    return record


# ---- per-user OAuth ----------------------------------------------------------------------------


async def start_connect(session: AsyncSession, principal: Principal) -> str:
    record = await _live_record(session, principal.company_id)
    if record.mode == IntegrationMode.MOCK:
        raise AppError("Mock mode needs no Microsoft sign-in.", code="mock_mode").with_status(409)
    graph = factory.graph_client(record, integrations.secrets_of(record))
    state = sign_state(
        {"u": str(principal.user_id), "c": str(principal.company_id), "n": uuid.uuid4().hex},
        purpose=STATE_PURPOSE,
    )
    return graph.authorization_url(state=state, redirect_uri=redirect_uri())


async def complete_connect(session: AsyncSession, *, code: str, state: str) -> None:
    data = verify_state(state, purpose=STATE_PURPOSE)
    user_id, company_id = uuid.UUID(data["u"]), uuid.UUID(data["c"])
    await set_tenant_context(session, TenantContext(company_id=company_id, user_id=user_id))
    await ensure_member(session, company_id, user_id, field="user")
    record = await _live_record(session, company_id)
    graph = factory.graph_client(record, integrations.secrets_of(record))
    tokens = await graph.exchange_code(code=code, redirect_uri=redirect_uri())
    if not tokens.refresh_token:
        raise AppError("Microsoft did not grant offline access", code="teams_no_refresh_token")
    from app.integrations.providers.teams import granted_scopes

    missing = missing_messaging_scopes(granted_scopes(tokens.access_token) or tokens.scopes)
    if missing:
        raise AppError(
            "Microsoft did not grant the required permissions: " + ", ".join(missing),
            code="teams_permissions_missing",
            details={"missing": missing},
        ).with_status(403)
    me = await graph.me(tokens.access_token)
    conn = await session.scalar(
        select(IntegrationUserConnection).where(
            IntegrationUserConnection.company_id == company_id,
            IntegrationUserConnection.user_id == user_id,
            IntegrationUserConnection.provider == Provider.MICROSOFT_TEAMS.value,
        )
    )
    if conn is None:
        conn = IntegrationUserConnection(
            company_id=company_id,
            user_id=user_id,
            provider=Provider.MICROSOFT_TEAMS.value,
            integration_id=record.id,
        )
        session.add(conn)
    conn.integration_id = record.id
    conn.external_user_id = me.id
    conn.account_email = me.email
    conn.encrypted_refresh_token = encrypt(tokens.refresh_token)
    conn.scopes = " ".join(sorted(tokens.scopes))[:1000]
    conn.status = "ACTIVE"
    conn.last_error = None
    audit.record(
        session,
        "teams.user_connected",
        company_id=company_id,
        actor_user_id=user_id,
        entity_type="integration_user_connection",
    )
    await session.commit()


async def get_connection(
    session: AsyncSession, principal: Principal
) -> IntegrationUserConnection | None:
    conn: IntegrationUserConnection | None = await session.scalar(
        select(IntegrationUserConnection).where(
            IntegrationUserConnection.company_id == principal.company_id,
            IntegrationUserConnection.user_id == principal.user_id,
            IntegrationUserConnection.provider == Provider.MICROSOFT_TEAMS.value,
        )
    )
    return conn


async def disconnect_user(session: AsyncSession, principal: Principal) -> None:
    conn = await get_connection(session, principal)
    if conn is None:
        return
    record = await integrations.get_record(session, principal.company_id, Provider.MICROSOFT_TEAMS)
    subs = await session.scalars(
        select(IntegrationSubscription).where(
            IntegrationSubscription.company_id == principal.company_id,
            IntegrationSubscription.user_connection_id == conn.id,
        )
    )
    if record is not None and record.mode == IntegrationMode.LIVE:
        for sub in subs:
            try:
                graph = factory.graph_client(record, integrations.secrets_of(record))
                token = await _access_token(session, conn, graph)
                await graph.delete_subscription(token, sub.external_subscription_id)
            except (ProviderError, factory.CredentialsMissingError):
                logger.warning("teams_subscription_delete_failed")
    await session.delete(conn)
    audit.record(
        session,
        "teams.user_disconnected",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration_user_connection",
    )
    await session.commit()


async def _access_token(
    session: AsyncSession, conn: IntegrationUserConnection, graph: GraphClient
) -> str:
    try:
        tokens = await graph.refresh(decrypt(conn.encrypted_refresh_token))
    except (ProviderAuthError, ValueError):
        conn.status = "REAUTH_REQUIRED"
        conn.last_error = "reauth_required"
        await session.commit()
        raise TeamsNotConnectedError(
            "Your Microsoft Teams sign-in expired. Please reconnect.", code="teams_reauth_required"
        ) from None
    if tokens.refresh_token and tokens.refresh_token != decrypt(conn.encrypted_refresh_token):
        conn.encrypted_refresh_token = encrypt(tokens.refresh_token)
    return tokens.access_token


async def _user_graph(
    session: AsyncSession, principal: Principal, record: Integration
) -> tuple[GraphClient, IntegrationUserConnection, str]:
    conn = await get_connection(session, principal)
    if conn is None or conn.status != "ACTIVE":
        raise TeamsNotConnectedError()
    graph = factory.graph_client(record, integrations.secrets_of(record))
    token = await _access_token(session, conn, graph)
    return graph, conn, token


async def message_provider_for(
    session: AsyncSession, principal: Principal, record: Integration
) -> MessageProvider:
    if record.mode == IntegrationMode.MOCK:
        return MockTeamsMessageProvider()
    graph, _, token = await _user_graph(session, principal, record)
    return TeamsMessageProvider(graph, token)


# ---- chats -------------------------------------------------------------------------------------


async def list_chats(session: AsyncSession, principal: Principal) -> list[TeamsChat]:
    record = await integrations.require_capability(
        session, principal.company_id, Provider.MICROSOFT_TEAMS, Capability.MESSAGE
    )
    if record.mode == IntegrationMode.MOCK:
        return list(MOCK_CHATS)
    graph, _, token = await _user_graph(session, principal, record)
    return await graph.list_chats(token)


async def link_chat(
    session: AsyncSession,
    principal: Principal,
    *,
    chat_id: str,
    contact_id: uuid.UUID,
    ip: str | None,
) -> CommunicationSession:
    """Attach one of the salesperson's Teams chats to a customer and subscribe to it."""
    record = await integrations.require_capability(
        session, principal.company_id, Provider.MICROSOFT_TEAMS, Capability.MESSAGE
    )
    contact = await ContactRepository(session).get(principal.company_id, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    graph: GraphClient | None = None
    conn: IntegrationUserConnection | None = None
    token = ""
    if record.mode == IntegrationMode.LIVE:
        graph, conn, token = await _user_graph(session, principal, record)
        if chat_id not in {c.id for c in await graph.list_chats(token, top=50)}:
            raise NotFoundError("Chat not found in your recent Teams chats")
    elif chat_id not in {c.id for c in MOCK_CHATS}:
        raise NotFoundError("Chat not found")
    existing = await session.scalar(
        select(ContactIdentity).where(
            ContactIdentity.company_id == principal.company_id,
            ContactIdentity.kind == IdentityKind.TEAMS_CHAT.value,
            ContactIdentity.value == chat_id,
        )
    )
    if existing is not None and existing.contact_id != contact.id:
        raise ConflictError(
            "This chat is already linked to another contact.", code="chat_linked_elsewhere"
        )
    comm = await conversations.get_or_create_session(
        session,
        principal.company_id,
        provider=Provider.MICROSOFT_TEAMS.value,
        channel=Channel.TEAMS,
        capability=Capability.MESSAGE,
        external_session_id=chat_id,
        integration_id=record.id,
        owner_user_id=principal.user_id,
        contact_id=contact.id,
    )
    comm.contact_id = contact.id
    comm.match_status = MatchStatus.MATCHED.value
    comm.owner_user_id = principal.user_id
    if existing is None:
        session.add(
            ContactIdentity(
                company_id=principal.company_id,
                contact_id=contact.id,
                kind=IdentityKind.TEAMS_CHAT.value,
                value=chat_id,
                created_by_user_id=principal.user_id,
            )
        )
    if graph is not None and conn is not None:
        resource = f"/chats/{chat_id}/messages"
        sub = await session.scalar(
            select(IntegrationSubscription).where(
                IntegrationSubscription.company_id == principal.company_id,
                IntegrationSubscription.integration_id == record.id,
                IntegrationSubscription.resource == resource,
                IntegrationSubscription.status == "ACTIVE",
            )
        )
        if sub is None:
            ext_id, expires = await graph.create_subscription(
                token,
                resource=resource,
                notification_url=notification_url(record.id),
                client_state=client_state_for(record.id),
            )
            session.add(
                IntegrationSubscription(
                    company_id=principal.company_id,
                    integration_id=record.id,
                    user_connection_id=conn.id,
                    provider=Provider.MICROSOFT_TEAMS.value,
                    resource=resource,
                    external_subscription_id=ext_id,
                    expires_at=expires,
                    status="ACTIVE",
                )
            )
    audit.record(
        session,
        "teams.chat_linked",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="communication_session",
        entity_id=comm.id,
        ip=ip,
    )
    await session.commit()
    return comm


# ---- notifications (job) -----------------------------------------------------------------------


@register_job("teams.message_notification")
async def _notification_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    if company_id is None:
        return
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        sub = await session.scalar(
            select(IntegrationSubscription).where(
                IntegrationSubscription.company_id == company_id,
                IntegrationSubscription.external_subscription_id == str(payload["subscription_id"]),
            )
        )
        if sub is None or sub.user_connection_id is None:
            return
        record = await session.scalar(
            select(Integration).where(
                Integration.company_id == company_id, Integration.id == sub.integration_id
            )
        )
        conn = await session.scalar(
            select(IntegrationUserConnection).where(
                IntegrationUserConnection.company_id == company_id,
                IntegrationUserConnection.id == sub.user_connection_id,
            )
        )
        if record is None or conn is None or conn.status != "ACTIVE":
            return
        if Capability.MESSAGE.value not in record.enabled_capabilities:
            return
        graph = factory.graph_client(record, integrations.secrets_of(record))
        try:
            token = await _access_token(session, conn, graph)
        except TeamsNotConnectedError:
            return
        chat_id = str(payload["chat_id"])
        if sub.resource != f"/chats/{chat_id}/messages":
            logger.warning("teams_notification_resource_mismatch")
            return
        msg = await graph.get_message(token, chat_id, str(payload["message_id"]))
        event = normalize_message(msg, chat_id=chat_id, our_user_id=conn.external_user_id or "")
        if event is None:
            return
        await conversations.ingest(
            session, company_id, event, integration_id=record.id, owner_user_id=conn.user_id
        )


@register_periodic("teams.renew_subscriptions", interval_seconds=900)
@register_job("teams.renew_subscriptions")
async def _renew_job(company_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
    """Renew chat-message subscriptions before they expire (they last ~1 hour)."""
    async with get_session_factory()() as session:
        routes = list(
            (
                await session.scalars(
                    select(IntegrationRoute).where(
                        IntegrationRoute.provider == Provider.MICROSOFT_TEAMS.value
                    )
                )
            ).all()
        )
    for route in routes:
        async with get_session_factory()() as session:
            await set_tenant_context(session, TenantContext(company_id=route.company_id))
            record = await session.scalar(
                select(Integration).where(
                    Integration.company_id == route.company_id,
                    Integration.id == route.integration_id,
                )
            )
            if record is None or record.mode != IntegrationMode.LIVE:
                continue
            due = await session.scalars(
                select(IntegrationSubscription).where(
                    IntegrationSubscription.company_id == route.company_id,
                    IntegrationSubscription.integration_id == record.id,
                    IntegrationSubscription.status == "ACTIVE",
                    IntegrationSubscription.expires_at < datetime.now(UTC) + RENEW_BEFORE,
                )
            )
            for sub in list(due.all()):
                conn = await session.scalar(
                    select(IntegrationUserConnection).where(
                        IntegrationUserConnection.company_id == route.company_id,
                        IntegrationUserConnection.id == sub.user_connection_id,
                    )
                )
                if conn is None:
                    sub.status = "FAILED"
                    sub.last_error = "no_user_connection"
                    continue
                try:
                    graph = factory.graph_client(record, integrations.secrets_of(record))
                    token = await _access_token(session, conn, graph)
                    sub.expires_at = await graph.renew_subscription(
                        token, sub.external_subscription_id
                    )
                    sub.last_error = None
                except (
                    ProviderError,
                    TeamsNotConnectedError,
                    factory.CredentialsMissingError,
                ) as exc:
                    sub.last_error = getattr(exc, "code", "error")[:64]
                    if sub.expires_at < datetime.now(UTC):
                        sub.status = "FAILED"
            await session.commit()


async def delete_all_subscriptions(
    session: AsyncSession, company_id: uuid.UUID, record: Integration
) -> None:
    subs = await session.scalars(
        select(IntegrationSubscription).where(
            IntegrationSubscription.company_id == company_id,
            IntegrationSubscription.integration_id == record.id,
            IntegrationSubscription.status == "ACTIVE",
        )
    )
    for sub in list(subs.all()):
        conn = await session.scalar(
            select(IntegrationUserConnection).where(
                IntegrationUserConnection.company_id == company_id,
                IntegrationUserConnection.id == sub.user_connection_id,
            )
        )
        if conn is None:
            continue
        try:
            graph = factory.graph_client(record, integrations.secrets_of(record))
            token = await _access_token(session, conn, graph)
            await graph.delete_subscription(token, sub.external_subscription_id)
        except (ProviderError, TeamsNotConnectedError, factory.CredentialsMissingError):
            logger.warning("teams_subscription_delete_failed")


# ---- admin consent (calling permissions) -------------------------------------------------------


async def admin_consent_url(session: AsyncSession, principal: Principal) -> str:
    if not principal.is_admin:
        raise AppError("Only admins can grant consent", code="forbidden").with_status(403)
    record = await _live_record(session, principal.company_id)
    graph = factory.graph_client(record, integrations.secrets_of(record))
    state = sign_state(
        {"u": str(principal.user_id), "c": str(principal.company_id)}, purpose=CONSENT_PURPOSE
    )
    return graph.admin_consent_url(state=state, redirect_uri=consent_redirect_uri())
