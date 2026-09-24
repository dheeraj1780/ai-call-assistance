"""Builds provider adapters from integration records (dependency injection point).

Business services ask this module for an adapter; they never construct HTTP/SDK clients.
Tests call ``set_http_transport`` to route every adapter through ``httpx.MockTransport`` and
use the shared mock instances below.
"""

import uuid

import httpx

from app.common.config import get_settings
from app.integrations.domain import IntegrationMode
from app.integrations.models import Integration
from app.integrations.providers.plivo import PlivoCredentials, PlivoTelephonyProvider
from app.integrations.providers.teams import (
    GraphClient,
    MockTeamsGateway,
    MockTeamsMessageProvider,
    TeamsAppCredentials,
    TeamsCallingProvider,
    TeamsGatewayClient,
)
from app.integrations.providers.whatsapp import (
    MockWhatsAppClient,
    WhatsAppClient,
    WhatsAppCredentials,
)
from app.telephony.provider import TelephonyProvider, get_telephony_provider

_transport: httpx.AsyncBaseTransport | None = None
_mock_teams_gateway = MockTeamsGateway()


class CredentialsMissingError(Exception):
    """A LIVE adapter was requested but required credentials are not stored."""


def set_http_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


def mock_teams_gateway() -> MockTeamsGateway:
    return _mock_teams_gateway


def reset_mocks() -> None:
    global _mock_teams_gateway
    _mock_teams_gateway = MockTeamsGateway()
    MockWhatsAppClient.sent = []
    MockWhatsAppClient.fail_next = None
    MockTeamsMessageProvider.sent = []


def _require(values: dict[str, str], *keys: str) -> list[str]:
    missing = [k for k in keys if not values.get(k)]
    if missing:
        raise CredentialsMissingError(", ".join(missing))
    return [values[k] for k in keys]


def whatsapp_client(
    integration: Integration, secrets: dict[str, str]
) -> WhatsAppClient | MockWhatsAppClient:
    if integration.mode == IntegrationMode.MOCK:
        return MockWhatsAppClient()
    values = {**integration.config, **secrets}
    pnid, waba, token, app_secret, verify = _require(
        values,
        "phone_number_id",
        "business_account_id",
        "access_token",
        "app_secret",
        "verify_token",
    )
    return WhatsAppClient(
        WhatsAppCredentials(pnid, waba, token, app_secret, verify), transport=_transport
    )


def teams_app_credentials(integration: Integration, secrets: dict[str, str]) -> TeamsAppCredentials:
    settings = get_settings()
    tenant = str(integration.config.get("tenant_id") or "")
    client_id = str(integration.config.get("client_id") or settings.microsoft_client_id or "")
    client_secret = secrets.get("client_secret") or (
        settings.microsoft_client_secret.get_secret_value()
        if settings.microsoft_client_secret is not None
        else ""
    )
    if not (tenant and client_id and client_secret):
        raise CredentialsMissingError("tenant_id / client_id / client_secret")
    return TeamsAppCredentials(tenant, client_id, client_secret)


def graph_client(integration: Integration, secrets: dict[str, str]) -> GraphClient:
    return GraphClient(teams_app_credentials(integration, secrets), transport=_transport)


def gateway_client() -> TeamsGatewayClient | None:
    s = get_settings()
    if not s.teams_media_gateway_url or s.teams_media_gateway_secret is None:
        return None
    return TeamsGatewayClient(
        s.teams_media_gateway_url, s.teams_media_gateway_secret.get_secret_value(), _transport
    )


def teams_calling_provider(integration: Integration) -> TeamsCallingProvider:
    persistence = str(integration.config.get("call_persistence") or "TRANSIENT")
    if integration.mode == IntegrationMode.MOCK:
        return TeamsCallingProvider(
            _mock_teams_gateway,
            tenant_id="mock-tenant",
            persistence=persistence,
            name="teams-mock",
        )
    gateway = gateway_client()
    if gateway is None:
        raise CredentialsMissingError("TEAMS_MEDIA_GATEWAY_URL / TEAMS_MEDIA_GATEWAY_SECRET")
    return TeamsCallingProvider(
        gateway, tenant_id=str(integration.config.get("tenant_id") or ""), persistence=persistence
    )


def plivo_provider(
    integration: Integration, secrets: dict[str, str], *, stream_enabled: bool
) -> TelephonyProvider:
    if integration.mode == IntegrationMode.MOCK:
        # MOCKED: the existing mock telephony provider + simulator.
        return get_telephony_provider()
    values = {**integration.config, **secrets}
    auth_id, token, number = _require(values, "auth_id", "auth_token", "phone_number")
    return PlivoTelephonyProvider(
        PlivoCredentials(auth_id, token, number, values.get("application_id") or None),
        integration_id=str(integration.id),
        stream_enabled=stream_enabled,
        transport=_transport,
    )


def integration_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
