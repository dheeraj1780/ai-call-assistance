"""Microsoft Teams adapters.

Messaging (Microsoft Graph, delegated per salesperson - least privilege):
  scopes  offline_access User.Read Chat.Read ChatMessage.Send
  - OAuth 2.0 authorization code flow against Microsoft Entra ID (tenant-specific authority)
  - GET /me, GET /me/chats, GET /chats/{id}/messages/{id}, POST /chats/{id}/messages
  - change notifications: POST /subscriptions on /chats/{id}/messages (no resource data, so no
    encryption certificate is needed; we fetch the message with the user's token). Chat-message
    subscriptions longer than 1 hour need lifecycle notifications, so we use ~55 minutes and
    renew them from a periodic job.
  Sending with application permissions is not possible (Microsoft only allows it for
  migration), which is why each salesperson connects their own account.

Calling (application permissions, admin consent): Calls.JoinGroupCall.All, Calls.AccessMedia.All.
Real-time media is NOT handled here: Microsoft's application-hosted media stack is C#/.NET on
Windows Server. The .NET ``teams-media-gateway`` joins the meeting, receives audio and streams it
to this API; ``TeamsGatewayClient`` is the signed internal HTTP contract with that service.

IMPLEMENTED + MOCK VERIFIED (httpx.MockTransport). EXTERNAL PROVIDER VERIFICATION REQUIRED.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.common.config import get_settings
from app.conversations.models import Direction, MessageStatus, MessageType, SenderType
from app.integrations.domain import Capability, Channel, ConversationEventType, Provider
from app.integrations.normalizer import (
    ConversationEvent,
    NormalizedMessage,
    ParticipantRef,
    html_to_text,
)
from app.integrations.providers.base import (
    ConnectionReport,
    OutboundMessage,
    ProviderAuthError,
    ProviderError,
    ProviderPermissionError,
    ProviderRequestError,
    ProviderUnavailableError,
    SentMessage,
    WebhookRejectedError,
    raise_for_status,
    request,
)
from app.telephony.provider import OutboundCallRequest, TelephonyError

NAME = "teams"
MESSAGING_SCOPES = ("offline_access", "User.Read", "Chat.Read", "ChatMessage.Send")
CALLING_ROLES = ("Calls.JoinGroupCall.All", "Calls.AccessMedia.All")
SUBSCRIPTION_MINUTES = 55
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


@dataclass(frozen=True)
class TeamsAppCredentials:
    tenant_id: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class DelegatedTokens:
    access_token: str
    refresh_token: str | None
    scopes: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class TeamsUser:
    id: str
    display_name: str | None
    email: str | None


@dataclass(frozen=True)
class TeamsChat:
    id: str
    topic: str | None
    chat_type: str
    last_updated: str | None


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def jwt_claims(token: str) -> dict[str, Any]:
    """Read claims of an access token we just received from Microsoft over TLS (for the
    granted roles/scopes only - never used to authenticate anyone)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


def granted_scopes(token: str) -> set[str]:
    scp = jwt_claims(token).get("scp", "")
    return set(str(scp).split()) if scp else set()


def granted_roles(token: str) -> set[str]:
    roles = jwt_claims(token).get("roles", [])
    return {str(r) for r in roles} if isinstance(roles, list) else set()


def missing_messaging_scopes(scopes: set[str]) -> list[str]:
    # offline_access is not reflected in the access token's scp claim.
    return [s for s in MESSAGING_SCOPES if s != "offline_access" and s not in scopes]


class GraphClient:
    def __init__(
        self, creds: TeamsAppCredentials, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.creds = creds
        s = get_settings()
        self._login = f"{s.microsoft_login_base_url.rstrip('/')}/{creds.tenant_id}"
        self._graph = s.microsoft_graph_base_url.rstrip("/")
        self._timeout = s.integration_http_timeout_seconds
        self._transport = transport

    def _http(self, token: str | None = None) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport, headers=headers)

    # -- OAuth ------------------------------------------------------------------------------

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.creds.client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "response_mode": "query",
                "scope": " ".join(MESSAGING_SCOPES),
                "state": state,
                "prompt": "select_account",
            }
        )
        return f"{self._login}/oauth2/v2.0/authorize?{query}"

    def admin_consent_url(self, *, state: str, redirect_uri: str) -> str:
        query = urllib.parse.urlencode(
            {
                "client_id": self.creds.client_id,
                "scope": GRAPH_DEFAULT_SCOPE,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{self._login}/v2.0/adminconsent?{query}"

    async def _token(self, data: dict[str, str], operation: str) -> dict[str, Any]:
        async with self._http() as http:
            resp = await request(
                http,
                "POST",
                f"{self._login}/oauth2/v2.0/token",
                provider=NAME,
                operation=operation,
                data={
                    **data,
                    "client_id": self.creds.client_id,
                    "client_secret": self.creds.client_secret,
                },
            )
        if resp.status_code in (400, 401):
            error = ""
            with contextlib.suppress(ValueError):
                error = str(resp.json().get("error", ""))
            if error in ("invalid_grant", "interaction_required", "consent_required"):
                raise ProviderAuthError("teams: reauthorization required")
            if error in ("invalid_client", "unauthorized_client", "invalid_request") or (
                resp.status_code == 401
            ):
                raise ProviderAuthError(f"teams: token request rejected ({error or 'error'})")
        raise_for_status(resp, provider=NAME)
        body: dict[str, Any] = resp.json()
        return body

    async def app_token(self) -> str:
        body = await self._token(
            {"grant_type": "client_credentials", "scope": GRAPH_DEFAULT_SCOPE}, "app_token"
        )
        return str(body["access_token"])

    async def exchange_code(self, *, code: str, redirect_uri: str) -> DelegatedTokens:
        body = await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "scope": " ".join(MESSAGING_SCOPES),
            },
            "exchange_code",
        )
        return DelegatedTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token"),
            scopes=set(str(body.get("scope", "")).split()),
        )

    async def refresh(self, refresh_token: str) -> DelegatedTokens:
        body = await self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(MESSAGING_SCOPES),
            },
            "refresh_token",
        )
        return DelegatedTokens(
            access_token=str(body["access_token"]),
            refresh_token=body.get("refresh_token") or refresh_token,
            scopes=set(str(body.get("scope", "")).split()),
        )

    # -- Graph (delegated) ------------------------------------------------------------------

    async def _get(self, token: str, path: str, operation: str, **params: str) -> dict[str, Any]:
        async with self._http(token) as http:
            resp = await request(
                http,
                "GET",
                f"{self._graph}{path}",
                provider=NAME,
                operation=operation,
                retries=1,
                params=params or None,
            )
        raise_for_status(resp, provider=NAME)
        body: dict[str, Any] = resp.json()
        return body

    async def me(self, token: str) -> TeamsUser:
        body = await self._get(
            token, "/me", "get_me", **{"$select": "id,displayName,mail,userPrincipalName"}
        )
        return TeamsUser(
            id=str(body["id"]),
            display_name=body.get("displayName"),
            email=(body.get("mail") or body.get("userPrincipalName") or None),
        )

    async def list_chats(self, token: str, top: int = 25) -> list[TeamsChat]:
        body = await self._get(
            token,
            "/me/chats",
            "list_chats",
            **{"$top": str(top), "$orderby": "lastMessagePreview/createdDateTime desc"},
        )
        return [
            TeamsChat(
                id=str(c["id"]),
                topic=c.get("topic"),
                chat_type=str(c.get("chatType", "")),
                last_updated=c.get("lastUpdatedDateTime"),
            )
            for c in body.get("value") or []
            if isinstance(c, dict) and c.get("id")
        ]

    async def get_message(self, token: str, chat_id: str, message_id: str) -> dict[str, Any]:
        return await self._get(
            token,
            f"/chats/{_q(chat_id)}/messages/{_q(message_id)}",
            "get_message",
        )

    async def send_chat_message(self, token: str, chat_id: str, text: str) -> SentMessage:
        async with self._http(token) as http:
            resp = await request(
                http,
                "POST",
                f"{self._graph}/chats/{urllib.parse.quote(chat_id, safe='')}/messages",
                provider=NAME,
                operation="send_message",
                json={"body": {"contentType": "text", "content": text}},
            )
        raise_for_status(resp, provider=NAME)
        try:
            body = resp.json()
            return SentMessage(external_message_id=str(body["id"])[:256], sent_at=datetime.now(UTC))
        except (ValueError, KeyError) as exc:
            raise ProviderRequestError("teams: unexpected send response") from exc

    async def create_subscription(
        self, token: str, *, resource: str, notification_url: str, client_state: str
    ) -> tuple[str, datetime]:
        expires = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        async with self._http(token) as http:
            resp = await request(
                http,
                "POST",
                f"{self._graph}/subscriptions",
                provider=NAME,
                operation="create_subscription",
                json={
                    "changeType": "created",
                    "notificationUrl": notification_url,
                    "resource": resource,
                    "includeResourceData": False,
                    "expirationDateTime": expires.isoformat().replace("+00:00", "Z"),
                    "clientState": client_state,
                },
            )
        raise_for_status(resp, provider=NAME)
        body = resp.json()
        return str(body["id"]), expires

    async def renew_subscription(self, token: str, subscription_id: str) -> datetime:
        expires = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
        async with self._http(token) as http:
            resp = await request(
                http,
                "PATCH",
                f"{self._graph}/subscriptions/{urllib.parse.quote(subscription_id, safe='')}",
                provider=NAME,
                operation="renew_subscription",
                json={"expirationDateTime": expires.isoformat().replace("+00:00", "Z")},
            )
        raise_for_status(resp, provider=NAME)
        return expires

    async def delete_subscription(self, token: str, subscription_id: str) -> None:
        async with self._http(token) as http:
            resp = await request(
                http,
                "DELETE",
                f"{self._graph}/subscriptions/{urllib.parse.quote(subscription_id, safe='')}",
                provider=NAME,
                operation="delete_subscription",
            )
        if resp.status_code != 404:
            raise_for_status(resp, provider=NAME)

    # -- connection test ----------------------------------------------------------------------

    async def test_connection(self, *, gateway: "TeamsGatewayClient | None") -> ConnectionReport:
        report = ConnectionReport()
        roles: set[str] = set()
        try:
            token = await self.app_token()
            roles = granted_roles(token)
            report.add("app_credentials", "Tenant ID, client ID and client secret are valid", True)
        except ProviderAuthError:
            report.add(
                "app_credentials",
                "Tenant ID, client ID and client secret are valid",
                False,
                "Microsoft Entra ID rejected the credentials (wrong tenant/client ID, expired "
                "secret, or the app is not consented in this tenant).",
            )
        except ProviderError as exc:
            report.add(
                "app_credentials",
                "Tenant ID, client ID and client secret are valid",
                False,
                f"Microsoft Entra ID could not be reached ({exc.code}).",
            )
        creds_ok = report.ok
        # Messaging uses delegated permissions consented by each salesperson when they connect.
        report.capabilities[Capability.MESSAGE.value] = creds_ok
        missing = [r for r in CALLING_ROLES if r not in roles]
        report.missing_permissions = missing if creds_ok else list(CALLING_ROLES)
        calling_ok = creds_ok and not missing
        report.add(
            "calling_permissions",
            "Calling permissions granted by an admin (Calls.JoinGroupCall.All, "
            "Calls.AccessMedia.All)",
            calling_ok,
            None
            if calling_ok
            else "Missing: "
            + ", ".join(report.missing_permissions)
            + ". Only needed for the real-time call copilot.",
            required=False,
        )
        gateway_ok = False
        if gateway is None:
            report.add(
                "media_gateway",
                "Teams media gateway reachable",
                False,
                "TEAMS_MEDIA_GATEWAY_URL / TEAMS_MEDIA_GATEWAY_SECRET are not configured.",
                required=False,
            )
        else:
            try:
                await gateway.health()
                gateway_ok = True
                report.add("media_gateway", "Teams media gateway reachable", True, required=False)
            except ProviderError as exc:
                report.add(
                    "media_gateway",
                    "Teams media gateway reachable",
                    False,
                    f"The media gateway did not answer its health check ({exc.code}).",
                    required=False,
                )
        report.capabilities[Capability.REAL_TIME_CALL.value] = calling_ok and gateway_ok
        return report


class TeamsMessageProvider:
    """Sends a Teams chat message as the connected salesperson."""

    name = NAME

    def __init__(self, graph: GraphClient, access_token: str) -> None:
        self.graph = graph
        self.access_token = access_token

    async def send_text(self, message: OutboundMessage) -> SentMessage:
        return await self.graph.send_chat_message(self.access_token, message.to, message.text)


class MockTeamsMessageProvider:
    """MOCKED: records Teams messages instead of sending them."""

    name = "teams-mock"
    sent: list[OutboundMessage] = []  # noqa: RUF012 - intentionally shared for tests/dev

    async def send_text(self, message: OutboundMessage) -> SentMessage:
        MockTeamsMessageProvider.sent.append(message)
        return SentMessage(external_message_id=f"mock-teams-{uuid.uuid4().hex}")


MOCK_CHATS = (
    TeamsChat(
        id="19:mock-chat-1@unq.gbl.spaces", topic=None, chat_type="oneOnOne", last_updated=None
    ),
    TeamsChat(
        id="19:mock-chat-2@thread.v2",
        topic="Quotation discussion",
        chat_type="group",
        last_updated=None,
    ),
)
MOCK_USER_ID = "00000000-0000-0000-0000-00000000a11c"


# ---- change notifications ---------------------------------------------------------------------


def client_state_for(integration_id: uuid.UUID) -> str:
    """Per-integration secret echoed back by Microsoft in every notification."""
    key = get_settings().jwt_secret.get_secret_value().encode()
    return hmac.new(
        key, f"teams-client-state:{integration_id}".encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class GraphNotification:
    subscription_id: str
    change_type: str
    resource: str
    chat_id: str
    message_id: str


def parse_notifications(body: bytes, integration_id: uuid.UUID) -> list[GraphNotification]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WebhookRejectedError("malformed json") from exc
    expected = client_state_for(integration_id)
    out: list[GraphNotification] = []
    for n in (data or {}).get("value") or []:
        if not isinstance(n, dict):
            continue
        if not hmac.compare_digest(str(n.get("clientState", "")), expected):
            raise WebhookRejectedError("bad clientState")
        resource = str(n.get("resource", ""))
        chat_id, message_id = _parse_resource(resource)
        if not chat_id or not message_id:
            continue
        out.append(
            GraphNotification(
                subscription_id=str(n.get("subscriptionId", ""))[:128],
                change_type=str(n.get("changeType", "")),
                resource=resource[:500],
                chat_id=chat_id,
                message_id=message_id,
            )
        )
    return out


def _parse_resource(resource: str) -> tuple[str | None, str | None]:
    # e.g. chats('19:...@unq.gbl.spaces')/messages('1612289765949')
    try:
        chat_part, msg_part = resource.split("/messages(", 1)
        chat_id = chat_part.split("chats(", 1)[1].strip("')")
        message_id = msg_part.strip("')")
        return chat_id, message_id
    except (IndexError, ValueError):
        return None, None


def normalize_message(
    msg: Mapping[str, Any], *, chat_id: str, our_user_id: str
) -> ConversationEvent | None:
    """Graph chatMessage -> ConversationEvent. System/event messages are skipped."""
    if msg.get("messageType") != "message" or msg.get("deletedDateTime"):
        return None
    sender = ((msg.get("from") or {}).get("user")) or {}
    sender_id = str(sender.get("id", ""))
    if not sender_id:
        return None
    body = msg.get("body") or {}
    content = str(body.get("content", ""))
    text = html_to_text(content) if body.get("contentType") == "html" else content.strip()
    if msg.get("attachments") and not text:
        text = "[Attachment]"
    ours = sender_id == our_user_id
    created = str(msg.get("createdDateTime", ""))
    try:
        occurred = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        occurred = datetime.now(UTC)
    msg_id = str(msg.get("id", ""))[:256]
    return ConversationEvent(
        type=ConversationEventType.MESSAGE_SENT if ours else ConversationEventType.MESSAGE_RECEIVED,
        provider=Provider.MICROSOFT_TEAMS,
        channel=Channel.TEAMS,
        capability=Capability.MESSAGE,
        external_event_id=f"msg:{msg_id}",
        external_session_id=chat_id[:256],
        occurred_at=occurred,
        participant=None
        if ours
        else ParticipantRef(
            role="CUSTOMER",
            external_id=sender_id[:256],
            display_name=(
                str(sender.get("displayName"))[:200] if sender.get("displayName") else None
            ),
        ),
        message=NormalizedMessage(
            external_message_id=msg_id,
            direction=Direction.OUTBOUND if ours else Direction.INBOUND,
            sender_type=SenderType.SALESPERSON if ours else SenderType.CUSTOMER,
            message_type=MessageType.TEXT if text else MessageType.UNSUPPORTED,
            text=text or "[Unsupported message]",
            status=MessageStatus.SENT if ours else MessageStatus.RECEIVED,
        ),
    )


# ---- Teams media gateway (internal, HMAC-signed) -----------------------------------------------


def sign_gateway(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-CC-Timestamp": ts, "X-CC-Signature": f"v1={digest}"}


def verify_gateway_signature(secret: str, headers: Mapping[str, str], body: bytes) -> None:
    ts = headers.get("x-cc-timestamp", "")
    sig = headers.get("x-cc-signature", "")
    if not ts.isdigit() or not sig.startswith("v1=") or not secret:
        raise WebhookRejectedError("missing signature")
    if abs(time.time() - int(ts)) > 300:
        raise WebhookRejectedError("stale request (possible replay)")
    expected = sign_gateway(secret, body, int(ts))["X-CC-Signature"]
    if not hmac.compare_digest(expected, sig):
        raise WebhookRejectedError("bad signature")


class TeamsGatewayClient:
    """HTTP contract with teams-media-gateway (see teams-media-gateway/README.md)."""

    def __init__(
        self, base_url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._secret = secret
        self._timeout = get_settings().integration_http_timeout_seconds
        self._transport = transport

    async def _send(
        self, method: str, path: str, payload: dict[str, Any] | None, op: str
    ) -> httpx.Response:
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json", **sign_gateway(self._secret, body)}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as http:
            resp = await request(
                http,
                method,
                f"{self._base}{path}",
                provider="teams-gateway",
                operation=op,
                content=body,
                headers=headers,
            )
        raise_for_status(resp, provider="teams-gateway")
        return resp

    async def health(self) -> None:
        await self._send("GET", "/health", None, "health")

    async def join(self, payload: dict[str, Any]) -> str:
        resp = await self._send("POST", "/v1/calls", payload, "join_call")
        try:
            return str(resp.json()["gateway_call_id"])[:128]
        except (ValueError, KeyError) as exc:
            raise ProviderRequestError("teams-gateway: unexpected join response") from exc

    async def leave(self, gateway_call_id: str) -> None:
        await self._send(
            "DELETE",
            f"/v1/calls/{urllib.parse.quote(gateway_call_id, safe='')}",
            None,
            "leave_call",
        )


class TeamsCallingProvider:
    """TelephonyProvider implementation for Teams meetings via the media gateway."""

    name = "teams"
    emits_end_events = False

    def __init__(
        self,
        gateway: "TeamsGatewayClient | MockTeamsGateway",
        *,
        tenant_id: str,
        persistence: str,
        name: str = "teams",
    ) -> None:
        self.gateway = gateway
        self.tenant_id = tenant_id
        self.persistence = persistence
        self.name = name
        self.emits_end_events = name == "teams-mock"

    async def create_call(self, request_: OutboundCallRequest) -> str:
        if not request_.meeting_url:
            raise TelephonyError("teams call without meeting url")
        base = get_settings().public_base_url.rstrip("/")
        try:
            return await self.gateway.join(
                {
                    "call_id": str(request_.call_id),
                    "tenant_id": self.tenant_id,
                    "join_url": request_.meeting_url,
                    "media_ws_url": request_.media_stream_url,
                    "events_url": f"{base}/api/v1/integrations/teams/gateway/events",
                    "persistence": self.persistence,
                    # The bot never speaks: receive-only audio.
                    "receive_only": True,
                }
            )
        except ProviderError as exc:
            raise TelephonyError(f"teams gateway join failed: {exc.code}") from exc

    async def end_call(self, provider_call_id: str) -> None:
        try:
            await self.gateway.leave(provider_call_id)
        except ProviderError as exc:
            raise TelephonyError(f"teams gateway leave failed: {exc.code}") from exc

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> None:
        raise WebhookRejectedError("use the Teams gateway events endpoint")

    def parse_webhook(self, body: bytes) -> list[Any]:
        return []

    def parse_media_message(self, message: str) -> Any:
        from app.telephony.provider import parse_json_media_message

        return parse_json_media_message(message)


class MockTeamsGateway:
    """MOCKED gateway: records join/leave requests; the Teams simulator plays the meeting."""

    def __init__(self) -> None:
        self.joined: list[dict[str, Any]] = []
        self.left: list[str] = []
        self.fail_join = False

    async def health(self) -> None:
        return None

    async def join(self, payload: dict[str, Any]) -> str:
        if self.fail_join:
            raise ProviderUnavailableError("mock gateway configured to fail")
        self.joined.append(payload)
        return f"mock-teams-{uuid.uuid4().hex}"

    async def leave(self, gateway_call_id: str) -> None:
        self.left.append(gateway_call_id)


def mock_test_report() -> ConnectionReport:
    report = ConnectionReport(account_label="Mock Microsoft 365 tenant")
    report.add("mock", "Mock mode: no request sent to Microsoft", True)
    report.capabilities[Capability.MESSAGE.value] = True
    report.capabilities[Capability.REAL_TIME_CALL.value] = True
    return report


def new_state_nonce() -> str:
    return secrets.token_urlsafe(16)


__all__ = [
    "CALLING_ROLES",
    "MESSAGING_SCOPES",
    "GraphClient",
    "MockTeamsGateway",
    "ProviderPermissionError",
    "TeamsAppCredentials",
    "TeamsCallingProvider",
    "TeamsGatewayClient",
    "TeamsMessageProvider",
]
