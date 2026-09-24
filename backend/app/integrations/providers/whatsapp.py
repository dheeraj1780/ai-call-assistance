"""WhatsApp Business Platform (Cloud API) adapter.

IMPLEMENTED against Meta's documented Cloud API (Graph API) and webhook formats:
- send:    POST {graph}/{version}/{phone_number_id}/messages  (type=text)
- test:    GET  {graph}/{version}/{phone_number_id}
           GET  {graph}/{version}/{waba_id}/phone_numbers
- webhook: GET verification (hub.mode / hub.verify_token / hub.challenge) and POST notifications
           signed with X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app_secret, raw body)
MOCK VERIFIED only (request shapes asserted with httpx.MockTransport). EXTERNAL PROVIDER
VERIFICATION REQUIRED with real credentials. WhatsApp Web automation is never used.

Policy enforced by the conversation service (not here): free-form messages only inside the
24-hour customer service window; every outbound message is an explicit human action.
"""

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.common.config import get_settings
from app.conversations.identity import normalize_phone
from app.conversations.models import Direction, MessageStatus, MessageType, SenderType
from app.integrations.domain import Capability, Channel, ConversationEventType, Provider
from app.integrations.normalizer import (
    ConversationEvent,
    NormalizedMessage,
    ParticipantRef,
    clip,
)
from app.integrations.providers.base import (
    ConnectionReport,
    OutboundMessage,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    SentMessage,
    WebhookRejectedError,
    raise_for_status,
    request,
)

NAME = "whatsapp"
MAX_TEXT = 4096  # Cloud API text body limit

_TYPE_MAP = {
    "text": MessageType.TEXT,
    "image": MessageType.IMAGE,
    "document": MessageType.DOCUMENT,
    "audio": MessageType.AUDIO,
    "voice": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "location": MessageType.LOCATION,
    "contacts": MessageType.CONTACTS,
    "interactive": MessageType.INTERACTIVE,
    "button": MessageType.INTERACTIVE,
    "sticker": MessageType.IMAGE,
}
_STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


@dataclass(frozen=True)
class WhatsAppCredentials:
    phone_number_id: str
    business_account_id: str
    access_token: str
    app_secret: str
    verify_token: str


def signature_for(app_secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(app_secret: str, headers: Mapping[str, str], body: bytes) -> None:
    received = headers.get("x-hub-signature-256", "")
    if not received.startswith("sha256=") or not app_secret:
        raise WebhookRejectedError("missing signature")
    if not hmac.compare_digest(signature_for(app_secret, body), received):
        raise WebhookRejectedError("bad signature")


def verify_challenge(verify_token: str, params: Mapping[str, str]) -> str:
    """GET verification handshake. Returns the challenge to echo back."""
    if params.get("hub.mode") != "subscribe":
        raise WebhookRejectedError("bad mode")
    if not verify_token or not hmac.compare_digest(
        params.get("hub.verify_token", ""), verify_token
    ):
        raise WebhookRejectedError("bad verify token")
    challenge = params.get("hub.challenge", "")
    if not challenge or len(challenge) > 256:
        raise WebhookRejectedError("bad challenge")
    return challenge


def _ts(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _message_text(msg: dict[str, Any], kind: str) -> str:
    if kind == "text":
        return clip(str((msg.get("text") or {}).get("body", "")))
    if kind in ("image", "video", "document"):
        caption = (msg.get(kind) or {}).get("caption")
        label = {"image": "[Image]", "video": "[Video]", "document": "[Document]"}[kind]
        return clip(f"{label} {caption}" if caption else label)
    if kind == "button":
        return clip(str((msg.get("button") or {}).get("text", "[Button reply]")))
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return clip(str(reply.get("title", "[Interactive reply]")))
    if kind == "location":
        return "[Location shared]"
    if kind in ("audio", "voice"):
        return "[Voice note - not transcribed]"
    if kind == "contacts":
        return "[Contact card shared]"
    if kind == "sticker":
        return "[Sticker]"
    return "[Unsupported message type]"


def parse_webhook(body: bytes, *, expected_phone_number_id: str) -> list[ConversationEvent]:
    """Normalise a Cloud API webhook. Changes for any other phone_number_id are ignored: an
    identifier inside the payload is never trusted to pick the tenant."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WebhookRejectedError("malformed json") from exc
    if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
        return []
    events: list[ConversationEvent] = []
    for entry in data.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if (change or {}).get("field") != "messages":
                continue
            value = change.get("value") or {}
            meta = value.get("metadata") or {}
            if str(meta.get("phone_number_id", "")) != expected_phone_number_id:
                continue
            names = {
                str(c.get("wa_id")): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
                if isinstance(c, dict)
            }
            for msg in value.get("messages") or []:
                wa_id = str(msg.get("from", ""))
                msg_id = str(msg.get("id", ""))
                if not wa_id or not msg_id:
                    continue
                kind = str(msg.get("type", "unsupported"))
                events.append(
                    ConversationEvent(
                        type=ConversationEventType.MESSAGE_RECEIVED,
                        provider=Provider.WHATSAPP,
                        channel=Channel.WHATSAPP,
                        capability=Capability.MESSAGE,
                        external_event_id=f"msg:{msg_id}",
                        external_session_id=wa_id,
                        occurred_at=_ts(msg.get("timestamp")),
                        participant=ParticipantRef(
                            role="CUSTOMER",
                            external_id=wa_id,
                            display_name=(str(names[wa_id])[:200] if names.get(wa_id) else None),
                            phone=normalize_phone("+" + wa_id),
                        ),
                        message=NormalizedMessage(
                            external_message_id=msg_id[:256],
                            direction=Direction.INBOUND,
                            sender_type=SenderType.CUSTOMER,
                            message_type=_TYPE_MAP.get(kind, MessageType.UNSUPPORTED),
                            text=_message_text(msg, kind),
                            status=MessageStatus.RECEIVED,
                        ),
                    )
                )
            for st in value.get("statuses") or []:
                msg_id = str(st.get("id", ""))
                status = _STATUS_MAP.get(str(st.get("status", "")))
                recipient = str(st.get("recipient_id", ""))
                if not msg_id or status is None or not recipient:
                    continue
                errors = st.get("errors") or []
                error_code = str(errors[0].get("code"))[:64] if errors else None
                events.append(
                    ConversationEvent(
                        type=ConversationEventType.MESSAGE_STATUS_UPDATED,
                        provider=Provider.WHATSAPP,
                        channel=Channel.WHATSAPP,
                        capability=Capability.MESSAGE,
                        external_event_id=f"status:{msg_id}:{status.value}",
                        external_session_id=recipient,
                        occurred_at=_ts(st.get("timestamp")),
                        data={
                            "external_message_id": msg_id[:256],
                            "status": status.value,
                            "error_code": error_code,
                        },
                    )
                )
    return events


class WhatsAppClient:
    name = NAME

    def __init__(
        self, creds: WhatsAppCredentials, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.creds = creds
        settings = get_settings()
        self._base = (
            f"{settings.whatsapp_graph_base_url.rstrip('/')}/{settings.whatsapp_graph_api_version}"
        )
        self._timeout = settings.integration_http_timeout_seconds
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.creds.access_token}"},
        )

    async def send_text(self, message: OutboundMessage) -> SentMessage:
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": message.to,
            "type": "text",
            "text": {"preview_url": False, "body": message.text[:MAX_TEXT]},
        }
        async with self._http() as http:
            # Not retried automatically: a retry could deliver the message twice.
            resp = await request(
                http,
                "POST",
                f"{self._base}/{self.creds.phone_number_id}/messages",
                provider=NAME,
                operation="send_message",
                json=body,
            )
        raise_for_status(resp, provider=NAME)
        try:
            msg_id = str(resp.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderRequestError("whatsapp: unexpected send response") from exc
        return SentMessage(external_message_id=msg_id[:256], sent_at=datetime.now(UTC))

    async def test_connection(self) -> ConnectionReport:
        report = ConnectionReport()
        async with self._http() as http:
            try:
                resp = await request(
                    http,
                    "GET",
                    f"{self._base}/{self.creds.phone_number_id}",
                    provider=NAME,
                    operation="get_phone_number",
                    retries=1,
                    params={"fields": "display_phone_number,verified_name,quality_rating"},
                )
                raise_for_status(resp, provider=NAME)
                info = resp.json()
                report.account_label = (
                    " ".join(
                        str(v)
                        for v in (info.get("verified_name"), info.get("display_phone_number"))
                        if v
                    )
                    or None
                )
                report.add("phone_number", "Access token can read the phone number", True)
            except ProviderAuthError:
                report.add(
                    "phone_number",
                    "Access token can read the phone number",
                    False,
                    "Meta rejected the access token (expired or revoked).",
                )
            except ProviderError as exc:
                report.add(
                    "phone_number",
                    "Access token can read the phone number",
                    False,
                    f"Meta returned an error ({exc.code}). Check the phone number ID and the "
                    "token's whatsapp_business_messaging permission.",
                )
            if report.ok:
                try:
                    resp = await request(
                        http,
                        "GET",
                        f"{self._base}/{self.creds.business_account_id}/phone_numbers",
                        provider=NAME,
                        operation="list_phone_numbers",
                        retries=1,
                        params={"fields": "id"},
                    )
                    raise_for_status(resp, provider=NAME)
                    ids = {str(p.get("id")) for p in resp.json().get("data") or []}
                    report.add(
                        "business_account",
                        "Phone number belongs to the WhatsApp Business Account",
                        self.creds.phone_number_id in ids,
                        None
                        if self.creds.phone_number_id in ids
                        else "The phone number ID is not listed under this business account.",
                    )
                except ProviderError as exc:
                    report.add(
                        "business_account",
                        "Phone number belongs to the WhatsApp Business Account",
                        False,
                        f"Could not list the account's numbers ({exc.code}); the token may lack "
                        "whatsapp_business_management.",
                    )
        report.capabilities[Capability.MESSAGE.value] = report.ok
        report.external_account_id = self.creds.phone_number_id
        return report


class MockWhatsAppClient:
    """MOCKED: records messages instead of sending them."""

    name = "whatsapp-mock"
    sent: list[OutboundMessage] = []  # noqa: RUF012 - intentionally shared for tests/dev
    fail_next: ProviderError | None = None

    async def send_text(self, message: OutboundMessage) -> SentMessage:
        if MockWhatsAppClient.fail_next is not None:
            exc, MockWhatsAppClient.fail_next = MockWhatsAppClient.fail_next, None
            raise exc
        MockWhatsAppClient.sent.append(message)
        return SentMessage(external_message_id=f"wamid.mock.{uuid.uuid4().hex}")

    async def test_connection(self) -> ConnectionReport:
        report = ConnectionReport(account_label="Mock WhatsApp number")
        report.add("mock", "Mock mode: no request sent to Meta", True)
        report.capabilities[Capability.MESSAGE.value] = True
        return report
