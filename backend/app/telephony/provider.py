"""Provider-neutral telephony interface and the mock provider.

Target flow (ADR-007): our app asks the provider to bridge the salesperson's phone and the
customer's phone and to fork a real-time media stream to our backend. The phone call lives
entirely at the provider; our backend, the browser and the AI can all fail without dropping
the call.

MockTelephonyProvider: MOCKED. It does not place real calls. It signs webhooks and media
tokens exactly like a real integration would be verified, so the webhook/idempotency/media
code paths are exercised end to end. LIVE PROVIDER INTEGRATION NOT VERIFIED (see
docs/TELEPHONY.md for the provider evaluation).
"""

import base64
import enum
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.common.config import get_settings


class ProviderCallState(enum.StrEnum):
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    CONNECTED = "CONNECTED"  # both legs answered
    ACTIVE = "ACTIVE"  # media flowing
    COMPLETED = "COMPLETED"
    NO_ANSWER = "NO_ANSWER"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Track(enum.StrEnum):
    """Which side of the bridged call an audio track belongs to."""

    AGENT = "agent"
    CUSTOMER = "customer"
    MIXED = "mixed"


class TelephonyError(Exception):
    code = "telephony_unavailable"


class WebhookVerificationError(Exception):
    pass


@dataclass(frozen=True)
class OutboundCallRequest:
    call_id: uuid.UUID
    agent_number: str
    customer_number: str
    status_callback_url: str
    media_stream_url: str


@dataclass(frozen=True)
class TelephonyEvent:
    event_id: str
    provider_call_id: str
    state: ProviderCallState
    occurred_at: datetime
    duration_seconds: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class MediaFrame:
    track: Track
    sequence: int
    audio: bytes


@dataclass(frozen=True)
class MediaControl:
    kind: str  # "start" | "stop"


class TelephonyProvider(Protocol):
    name: str

    async def create_call(self, request: OutboundCallRequest) -> str: ...
    async def end_call(self, provider_call_id: str) -> None: ...
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> None: ...
    def parse_webhook(self, body: bytes) -> list[TelephonyEvent]: ...
    def parse_media_message(self, message: str) -> MediaFrame | MediaControl | None: ...


def webhook_secret() -> bytes:
    settings = get_settings()
    if settings.telephony_webhook_secret is not None:
        return settings.telephony_webhook_secret.get_secret_value().encode()
    # Development/test only; production requires TELEPHONY_WEBHOOK_SECRET (config validation).
    return hashlib.sha256(
        b"dev-telephony:" + settings.jwt_secret.get_secret_value().encode()
    ).digest()


REPLAY_WINDOW_SECONDS = 300


def sign_mock_webhook(body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(webhook_secret(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class MockTelephonyProvider:
    name = "mock"

    def __init__(self) -> None:
        self.created: list[OutboundCallRequest] = []
        self.ended: list[str] = []
        self.fail_create: bool = False

    async def create_call(self, request: OutboundCallRequest) -> str:
        if self.fail_create:
            raise TelephonyError("mock provider configured to fail")
        self.created.append(request)
        return f"mock-{uuid.uuid4().hex}"

    async def end_call(self, provider_call_id: str) -> None:
        self.ended.append(provider_call_id)

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> None:
        header = headers.get("x-mock-signature", "")
        try:
            parts = dict(p.split("=", 1) for p in header.split(","))
            ts = int(parts["t"])
            signature = parts["v1"]
        except (ValueError, KeyError):
            raise WebhookVerificationError("missing or malformed signature") from None
        if abs(time.time() - ts) > REPLAY_WINDOW_SECONDS:
            raise WebhookVerificationError("stale webhook (possible replay)")
        expected = sign_mock_webhook(body, ts).split("v1=", 1)[1]
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("bad signature")

    def parse_webhook(self, body: bytes) -> list[TelephonyEvent]:
        try:
            data: dict[str, Any] = json.loads(body)
            return [
                TelephonyEvent(
                    event_id=str(data["event_id"])[:128],
                    provider_call_id=str(data["call_sid"])[:128],
                    state=ProviderCallState(data["status"]),
                    occurred_at=datetime.fromisoformat(data["timestamp"])
                    if data.get("timestamp")
                    else datetime.now(UTC),
                    duration_seconds=int(data["duration"])
                    if data.get("duration") is not None
                    else None,
                    error_code=str(data["error"])[:64] if data.get("error") else None,
                )
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise WebhookVerificationError("malformed webhook payload") from exc

    def parse_media_message(self, message: str) -> MediaFrame | MediaControl | None:
        try:
            data = json.loads(message)
        except ValueError:
            return None
        event = data.get("event")
        if event in ("start", "stop"):
            return MediaControl(kind=event)
        if event == "media":
            try:
                return MediaFrame(
                    track=Track(data["track"]),
                    sequence=int(data["seq"]),
                    audio=base64.b64decode(data["payload"], validate=True),
                )
            except (ValueError, KeyError):
                return None
        return None


_provider: TelephonyProvider | None = None


def get_telephony_provider() -> TelephonyProvider:
    global _provider
    if _provider is None:
        _provider = MockTelephonyProvider()
    return _provider


def set_telephony_provider(provider: TelephonyProvider) -> None:
    global _provider
    _provider = provider


def media_token(call_id: uuid.UUID, ttl_seconds: int = 4 * 3600) -> str:
    exp = int(time.time()) + ttl_seconds
    digest = hmac.new(
        webhook_secret(), f"media.{call_id}.{exp}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{exp}.{digest}"


def verify_media_token(call_id: uuid.UUID, token: str) -> bool:
    try:
        exp_s, digest = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(
        webhook_secret(), f"media.{call_id}.{exp}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, digest)
