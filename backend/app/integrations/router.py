"""Integrations API (Settings > Integrations).

GET responses never contain secret values: secret fields report only ``is_set``.
Configuration, tests, enabling and disconnecting are OWNER/ADMIN only.
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.config import get_settings
from app.common.crypto import verify_state
from app.common.db import get_db_session
from app.common.errors import AppError, ErrorResponse, NotFoundError
from app.common.rate_limit import client_ip
from app.integrations import service, teams_service
from app.integrations.domain import (
    PROVIDER_SLUGS,
    SLUG_OF,
    Capability,
    IntegrationMode,
    Provider,
)
from app.integrations.providers.base import ProviderError
from app.integrations.providers.factory import CredentialsMissingError
from app.integrations.secrets import mask

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)


def _provider(slug: str) -> Provider:
    provider = PROVIDER_SLUGS.get(slug)
    if provider is None:
        raise NotFoundError("Unknown integration")
    return provider


def _provider_error(exc: ProviderError) -> AppError:
    return AppError(
        "The provider could not complete the request. Please try again later.", code=exc.code
    ).with_status(502)


class CapabilityOut(BaseModel):
    capability: str
    label: str
    description: str
    channel: str
    state: str
    enabled: bool
    available: bool
    reasons: list[str]


class FieldOut(BaseModel):
    key: str
    label: str
    kind: str
    required: bool
    help: str
    pattern: str | None
    max_length: int
    options: list[dict[str, str]]
    value: str | None  # non-secret fields only
    is_set: bool
    display: str | None  # "********" for stored secrets


class IntegrationOut(BaseModel):
    provider: str
    slug: str
    name: str
    subtitle: str
    channel: str
    mode: str
    status: str
    configured: bool
    validated: bool
    enabled: bool
    capabilities: list[CapabilityOut]
    last_tested_at: str | None
    error_state: str | None
    mock_allowed: bool


class IntegrationDetailOut(IntegrationOut):
    fields: list[FieldOut]
    requirements: list[dict[str, Any]]
    last_test: dict[str, Any] | None
    setup: dict[str, Any]
    docs: str


def _summary(view: service.IntegrationView) -> IntegrationOut:
    record = view.record
    test = service._current_test(record)
    return IntegrationOut(
        provider=view.spec.provider.value,
        slug=view.spec.slug,
        name=view.spec.name,
        subtitle=view.spec.subtitle,
        channel=view.spec.channel.value,
        mode=view.mode.value,
        status=view.status.value,
        configured=view.status.value != "NOT_CONFIGURED",
        validated=bool(test and test.get("ok")),
        enabled=any(c.enabled for c in view.capabilities),
        capabilities=[
            CapabilityOut(
                capability=c.capability.value,
                label=c.label,
                description=c.description,
                channel=c.channel,
                state=c.state.value,
                enabled=c.enabled,
                available=c.available,
                reasons=c.reasons,
            )
            for c in view.capabilities
        ],
        last_tested_at=record.last_tested_at.isoformat()
        if record and record.last_tested_at
        else None,
        error_state=record.error_code if record else None,
        mock_allowed=get_settings().integration_mock_allowed,
    )


def _setup(view: service.IntegrationView) -> dict[str, Any]:
    base = get_settings().public_base_url.rstrip("/")
    record = view.record
    iid = str(record.id) if record else None
    if view.spec.provider == Provider.WHATSAPP:
        return {
            "webhook_url": f"{base}/api/v1/integrations/whatsapp/webhooks/{iid}" if iid else None,
            "webhook_fields": ["messages"],
            "note": "Use the same verify token you saved here. Save the configuration first "
            "to get the URL.",
        }
    if view.spec.provider == Provider.MICROSOFT_TEAMS:
        return {
            "oauth_redirect_uri": teams_service.redirect_uri(),
            "admin_consent_redirect_uri": teams_service.consent_redirect_uri(),
            "notification_url": teams_service.notification_url(record.id) if record else None,
            "gateway_events_url": f"{base}/api/v1/integrations/teams/gateway/events",
            "delegated_permissions": [
                "offline_access",
                "User.Read",
                "Chat.Read",
                "ChatMessage.Send",
            ],
            "application_permissions": ["Calls.JoinGroupCall.All", "Calls.AccessMedia.All"],
        }
    if view.spec.provider == Provider.GOOGLE_MEET:
        from app.integrations import google_meet_service
        from app.integrations.providers.google_meet import REQUIRED_SCOPES

        return {
            "oauth_redirect_uri": google_meet_service.redirect_uri(),
            "oauth_client_type": "Web application",
            "oauth_scopes": ["openid", "email", *REQUIRED_SCOPES],
            "apis_to_enable": ["Google Meet REST API (meet.googleapis.com)"],
            "developer_preview": "Meet Media API: the Google Cloud project, the OAuth user and all "
            "meeting participants must be enrolled in the Google Workspace Developer Preview "
            "Program.",
        }
    return {
        "inbound_answer_url": f"{base}/api/v1/integrations/plivo/webhooks/{iid}/inbound"
        if iid
        else None,
        "inbound_hangup_url": f"{base}/api/v1/integrations/plivo/webhooks/{iid}/hangup"
        if iid
        else None,
        "note": "Outbound call callback URLs are generated per call automatically.",
    }


def _detail(view: service.IntegrationView) -> IntegrationDetailOut:
    record = view.record
    keys = set(record.secret_keys) if record else set()
    fields = []
    for f in view.spec.fields:
        is_secret = f.kind == "secret"
        value = None if is_secret or record is None else record.config.get(f.key)
        fields.append(
            FieldOut(
                key=f.key,
                label=f.label,
                kind=f.kind,
                required=f.required,
                help=f.help,
                pattern=f.pattern,
                max_length=f.max_length,
                options=[{"value": v, "label": lbl} for v, lbl in f.options],
                value=str(value)
                if value is not None
                else (f.default if f.kind == "select" else None),
                is_set=(f.key in keys) if is_secret else value is not None,
                display=mask("x") if is_secret and f.key in keys else None,
            )
        )
    return IntegrationDetailOut(
        **_summary(view).model_dump(),
        fields=fields,
        requirements=[r.as_dict() for r in view.requirements],
        last_test=service._current_test(record),
        setup=_setup(view),
        docs=view.spec.docs,
    )


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> list[IntegrationOut]:
    return [_summary(v) for v in await service.list_views(session, principal)]


@router.get("/{slug}", response_model=IntegrationDetailOut)
async def get_integration(
    slug: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> IntegrationDetailOut:
    return _detail(await service.view_for(session, principal, _provider(slug)))


@router.get("/{slug}/config-check")
async def config_check(
    slug: str,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """What still needs to be configured (the frontend shows this as a checklist)."""
    view = await service.view_for(session, principal, _provider(slug))
    missing = [r for r in view.requirements if not r.ok]
    return {
        "provider": view.spec.provider.value,
        "status": view.status.value,
        "complete": not missing,
        "requirements": [r.as_dict() for r in view.requirements],
        "capabilities": {
            c.capability.value: {"state": c.state.value, "reasons": c.reasons}
            for c in view.capabilities
        },
    }


SecretOrText = Annotated[str, StringConstraints(max_length=1024)]


class ConfigureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: IntegrationMode = IntegrationMode.LIVE
    # Only fields being changed need to be sent. Secret fields: a value replaces the stored
    # secret; omitted/null keeps it; use ``clear_secrets`` to remove one.
    values: dict[str, SecretOrText | None] = Field(default_factory=dict, max_length=20)
    clear_secrets: list[str] = Field(default_factory=list, max_length=20)


@router.put(
    "/{slug}/config",
    response_model=IntegrationDetailOut,
    responses={409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def configure(
    slug: str,
    body: ConfigureIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> IntegrationDetailOut:
    view = await service.configure(
        session,
        principal,
        _provider(slug),
        mode=body.mode,
        values=body.values,
        clear_secrets=body.clear_secrets,
        ip=client_ip(request),
    )
    return _detail(view)


@router.post("/{slug}/test", response_model=IntegrationDetailOut)
async def run_connection_test(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> IntegrationDetailOut:
    view, _ = await service.run_test(session, principal, _provider(slug), ip=client_ip(request))
    return _detail(view)


@router.post(
    "/{slug}/capabilities/{capability}/{action}",
    response_model=IntegrationDetailOut,
    responses={409: {"model": ErrorResponse}},
)
async def set_capability(
    slug: str,
    capability: Capability,
    action: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> IntegrationDetailOut:
    if action not in ("enable", "disable"):
        raise NotFoundError("Unknown action")
    view = await service.set_capability(
        session,
        principal,
        _provider(slug),
        capability,
        enabled=action == "enable",
        ip=client_ip(request),
    )
    return _detail(view)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    slug: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await service.disconnect(session, principal, _provider(slug), ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Microsoft Teams: per-user sign-in, chats, admin consent -----------------------------------


class ConnectionOut(BaseModel):
    connected: bool
    account_email: str | None = None
    status: str | None = None


@router.get("/microsoft-teams/connection", response_model=ConnectionOut)
async def teams_connection(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> ConnectionOut:
    conn = await teams_service.get_connection(session, principal)
    if conn is None:
        return ConnectionOut(connected=False)
    return ConnectionOut(
        connected=conn.status == "ACTIVE", account_email=conn.account_email, status=conn.status
    )


@router.post("/microsoft-teams/connect")
async def teams_connect(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> dict[str, str]:
    try:
        return {"authorization_url": await teams_service.start_connect(session, principal)}
    except CredentialsMissingError as exc:
        raise service.IntegrationNotReadyError(f"Teams sign-in is not configured: {exc}") from None


@router.get("/microsoft-teams/oauth/callback", include_in_schema=False)
async def teams_oauth_callback(
    code: str | None = Query(default=None, max_length=4000),
    state: str | None = Query(default=None, max_length=2000),
    error: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    frontend = get_settings().frontend_base_url.rstrip("/")
    target = f"{frontend}/settings/integrations"
    if error or not code or not state:
        return RedirectResponse(f"{target}?teams=denied", status_code=303)
    try:
        await teams_service.complete_connect(session, code=code, state=state)
    except ValueError:
        return RedirectResponse(f"{target}?teams=invalid_state", status_code=303)
    except AppError as exc:
        return RedirectResponse(f"{target}?teams={exc.code}", status_code=303)
    except ProviderError as exc:
        logger.warning("teams_connect_failed", extra={"error": exc.code})
        return RedirectResponse(f"{target}?teams=connect_failed", status_code=303)
    return RedirectResponse(f"{target}?teams=connected", status_code=303)


@router.delete("/microsoft-teams/connection", status_code=status.HTTP_204_NO_CONTENT)
async def teams_disconnect(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> Response:
    await teams_service.disconnect_user(session, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/microsoft-teams/admin-consent-url")
async def teams_admin_consent_url(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> dict[str, str]:
    try:
        return {"url": await teams_service.admin_consent_url(session, principal)}
    except CredentialsMissingError as exc:
        raise service.IntegrationNotReadyError(f"Teams is not configured: {exc}") from None


@router.get("/microsoft-teams/admin-consent/callback", include_in_schema=False)
async def teams_admin_consent_callback(
    admin_consent: str | None = Query(default=None, max_length=10),
    state: str | None = Query(default=None, max_length=2000),
) -> RedirectResponse:
    frontend = get_settings().frontend_base_url.rstrip("/")
    ok = admin_consent == "True" and state is not None
    if ok and state is not None:
        try:
            verify_state(state, purpose=teams_service.CONSENT_PURPOSE)
        except ValueError:
            ok = False
    # Consent is re-verified by the next connection test (granted roles in the app token).
    return RedirectResponse(
        f"{frontend}/settings/integrations?consent={'granted' if ok else 'failed'}", status_code=303
    )


class ChatOut(BaseModel):
    id: str
    topic: str | None
    chat_type: str


@router.get("/microsoft-teams/chats", response_model=list[ChatOut])
async def teams_chats(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_db_session)
) -> list[ChatOut]:
    try:
        chats = await teams_service.list_chats(session, principal)
    except ProviderError as exc:
        raise _provider_error(exc) from None
    return [ChatOut(id=c.id, topic=c.topic, chat_type=c.chat_type) for c in chats]


class LinkChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: Annotated[str, StringConstraints(min_length=3, max_length=256)]
    contact_id: uuid.UUID


@router.post("/microsoft-teams/chats/link")
async def teams_link_chat(
    body: LinkChatIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    try:
        comm = await teams_service.link_chat(
            session,
            principal,
            chat_id=body.chat_id,
            contact_id=body.contact_id,
            ip=client_ip(request),
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    return {"session_id": str(comm.id)}


__all__ = ["SLUG_OF", "router"]
