"""Plivo voice adapter (PSTN calls + real-time audio streaming).

IMPLEMENTED against Plivo's Voice API (REST + XML + audio streams):
- create call:  POST {api}/v1/Account/{auth_id}/Call/   (Plivo rings the salesperson first)
- answer XML:   <Dial> to the customer (callbackUrl reports the customer's answer)
- audio:        Audio Streams REST API, started ONLY once the customer answered:
                POST {api}/v1/Account/{auth_id}/Call/{call_uuid}/Stream/ (both tracks,
                unidirectional, mu-law 8 kHz). Not <Stream> in the answer XML: Plivo's documented
                keepCallAlive semantics make a <Stream> element run exclusively until the stream
                disconnects (blocking the <Dial> that follows), and an XML stream would also start
                while the customer is still ringing (ringback would be transcribed).
- hang up:      DELETE {api}/v1/Account/{auth_id}/Call/{call_uuid}/  (or /Request/{uuid}/)
- test:         GET {api}/v1/Account/{auth_id}/  and  GET .../Number/{number}/
- signatures:   X-Plivo-Signature-V3 / -Nonce, ported from the official plivo-python SDK
                (plivo/utils/signature_v3.py, v4.62.0) and tested against vectors it produces.
MOCK VERIFIED only. EXTERNAL PROVIDER VERIFICATION REQUIRED (real account, public URL). No
paid call is ever placed by tests or by this code without an explicit user action.

The AI never speaks: the stream is unidirectional (listen-only) and nothing is played to the
caller. NO Plivo recording is used (no <Record>, no recording API); raw audio is passed to STT and
dropped (app/live/session.py). Verified against Plivo's public documentation (Stream XML and Audio
Streams API, Dial callback parameters, stream WebSocket protocol) on 2026-09-30; REAL ACCOUNT
VERIFICATION still required.
"""

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.common.config import get_settings
from app.integrations.domain import Capability
from app.integrations.providers.base import (
    ConnectionReport,
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    WebhookRejectedError,
    raise_for_status,
    request,
)
from app.telephony.provider import (
    MediaControl,
    MediaFrame,
    OutboundCallRequest,
    ProviderCallState,
    TelephonyError,
    Track,
)

NAME = "plivo"


@dataclass(frozen=True)
class PlivoCredentials:
    auth_id: str
    auth_token: str
    phone_number: str
    application_id: str | None = None


# ---- signature V3 (port of plivo/utils/signature_v3.py) -----------------------------------------


def _sorted_query_string(params: Mapping[str, list[str] | str]) -> str:
    parts = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, list):
            parts.append("&".join(f"{key}={v}" for v in sorted(value)))
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def _sorted_params_string(params: Mapping[str, list[str] | str]) -> str:
    parts = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, list):
            parts.append("".join(f"{key}{v}" for v in sorted(value)))
        else:
            parts.append(f"{key}{value}")
    return "".join(parts)


def _base_url(uri: str, method: str, params: Mapping[str, list[str] | str]) -> str:
    parsed = urlparse(uri)
    base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    query: dict[str, list[str] | str] = dict(parse_qs(parsed.query, keep_blank_values=True))
    if method == "GET":
        merged: dict[str, list[str] | str] = {**params, **query}
        qs = _sorted_query_string(merged)
        return f"{base}?{qs}" if qs else base
    qs = _sorted_query_string(query)
    if not params:
        return f"{base}?{qs}" if qs else base
    url = f"{base}?{qs}"
    if qs:
        url += "."
    return url + _sorted_params_string(params)


def compute_signature_v3(
    auth_token: str, uri: str, nonce: str, method: str, params: Mapping[str, list[str] | str]
) -> str:
    payload = f"{_base_url(uri, method.upper(), params)}.{nonce}".encode()
    digest = hmac.new(auth_token.encode(), payload, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_signature_v3(
    auth_token: str,
    *,
    uri: str,
    method: str,
    headers: Mapping[str, str],
    params: Mapping[str, list[str] | str],
) -> None:
    signature = headers.get("x-plivo-signature-v3", "")
    nonce = headers.get("x-plivo-signature-v3-nonce", "")
    if not signature or not nonce or not auth_token:
        raise WebhookRejectedError("missing signature")
    expected = compute_signature_v3(auth_token, uri, nonce, method, params)
    if not any(hmac.compare_digest(expected, s.strip()) for s in signature.split(",")):
        raise WebhookRejectedError("bad signature")


# ---- XML -------------------------------------------------------------------------------------


def answer_xml(
    *,
    customer_number: str,
    caller_id: str,
    dial_action_url: str,
    dial_callback_url: str,
) -> str:
    """Bridge the answered leg to the other party. The audio stream is NOT part of this XML: it is
    started through the Audio Streams API once the dial callback reports the answer.

    On the salesperson (A) leg, the ``inbound`` track is the salesperson and the ``outbound``
    track is what they hear - the customer."""
    dial = (
        f"<Dial callerId={quoteattr(caller_id)} action={quoteattr(dial_action_url)} "
        f'method="POST" callbackUrl={quoteattr(dial_callback_url)} callbackMethod="POST">'
        f"<Number>{escape(customer_number)}</Number></Dial>"
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{dial}</Response>'


def hangup_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def empty_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response/>'


# ---- callbacks -> provider-neutral call states ------------------------------------------------


@dataclass(frozen=True)
class PlivoCallback:
    kind: str  # ring | answer | dial | dial_action | hangup | stream
    call_uuid: str
    state: ProviderCallState | None
    event_id: str
    duration: int | None = None
    error: str | None = None


_HANGUP_STATES = {
    "completed": ProviderCallState.COMPLETED,
    "busy": ProviderCallState.NO_ANSWER,
    "no-answer": ProviderCallState.NO_ANSWER,
    "timeout": ProviderCallState.NO_ANSWER,
    "failed": ProviderCallState.FAILED,
    "cancel": ProviderCallState.CANCELLED,
    "canceled": ProviderCallState.CANCELLED,
}


def _first(params: Mapping[str, list[str] | str], key: str) -> str:
    value = params.get(key, "")
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def parse_callback(kind: str, params: Mapping[str, list[str] | str]) -> PlivoCallback:
    # Dial callbacks identify the salesperson's leg as DialALegUUID (CallUUID may be absent).
    call_uuid = (_first(params, "CallUUID") or _first(params, "DialALegUUID"))[:128]
    if not call_uuid:
        raise WebhookRejectedError("missing CallUUID")
    status = _first(params, "CallStatus").lower()
    if kind == "ring":
        return PlivoCallback(kind, call_uuid, ProviderCallState.RINGING, f"ring:{call_uuid}")
    if kind == "answer":
        # The salesperson picked up; the customer is being dialled.
        return PlivoCallback(kind, call_uuid, ProviderCallState.RINGING, f"answer:{call_uuid}")
    if kind == "dial":
        action = _first(params, "DialAction").lower()
        if action in ("answer", "connected"):
            return PlivoCallback(
                kind, call_uuid, ProviderCallState.CONNECTED, f"dial-answer:{call_uuid}"
            )
        return PlivoCallback(kind, call_uuid, None, f"dial-{action or 'event'}:{call_uuid}")
    if kind == "dial_action":
        dial_status = _first(params, "DialStatus").lower()
        state = None
        if dial_status in ("busy", "no-answer", "timeout", "cancel"):
            state = ProviderCallState.NO_ANSWER
        elif dial_status == "failed":
            state = ProviderCallState.FAILED
        return PlivoCallback(
            kind,
            call_uuid,
            state,
            f"dial-status:{call_uuid}:{dial_status or 'unknown'}",
            error=f"dial_{dial_status}"[:64] if state == ProviderCallState.FAILED else None,
        )
    if kind == "hangup":
        state = _HANGUP_STATES.get(status, ProviderCallState.COMPLETED)
        duration_raw = _first(params, "Duration")
        duration = int(duration_raw) if duration_raw.isdigit() else None
        cause = _first(params, "HangupCause")[:64] or None
        return PlivoCallback(
            kind,
            call_uuid,
            state,
            f"hangup:{call_uuid}",
            duration=duration,
            error=cause if state == ProviderCallState.FAILED else None,
        )
    if kind == "stream":
        event = _first(params, "Event")[:32] or "stream"
        return PlivoCallback(kind, call_uuid, None, f"stream:{call_uuid}:{event}")
    raise WebhookRejectedError("unknown callback kind")


# Plivo audio stream messages (JSON over WebSocket). Track mapping on the salesperson leg:
# inbound = salesperson, outbound = what the salesperson hears (the customer).
_TRACKS = {"inbound": Track.AGENT, "outbound": Track.CUSTOMER}


def parse_media_message(
    message: str, *, swap_tracks: bool = False
) -> MediaFrame | MediaControl | None:
    """``swap_tracks``: for inbound calls the answered leg is the customer."""
    try:
        data = json.loads(message)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if event == "start":
        fmt = (data.get("start") or {}).get("mediaFormat") or {}
        encoding = "mulaw" if "mulaw" in str(fmt.get("encoding", "audio/x-mulaw")) else "linear16"
        try:
            rate = int(fmt.get("sampleRate", 8000))
        except (TypeError, ValueError):
            rate = 8000
        return MediaControl(kind="start", encoding=encoding, sample_rate=rate)
    if event == "stop":
        return MediaControl(kind="stop")
    if event == "media":
        media = data.get("media") or {}
        track = _TRACKS.get(str(media.get("track", "")))
        if track is None:
            return None
        if swap_tracks:
            track = Track.CUSTOMER if track == Track.AGENT else Track.AGENT
        try:
            return MediaFrame(
                track=track,
                sequence=int(data.get("sequenceNumber", 0)),
                audio=base64.b64decode(str(media.get("payload", "")), validate=True),
            )
        except ValueError:
            return None
    return None


# ---- REST client + TelephonyProvider implementation ---------------------------------------------


class PlivoTelephonyProvider:
    """Implements the app's TelephonyProvider protocol for ONE company's Plivo account."""

    name = NAME
    emits_end_events = False

    def __init__(
        self,
        creds: PlivoCredentials,
        *,
        integration_id: str,
        stream_enabled: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.creds = creds
        self.integration_id = integration_id
        self.stream_enabled = stream_enabled
        settings = get_settings()
        self._base = f"{settings.plivo_api_base_url.rstrip('/')}/v1/Account/{creds.auth_id}"
        self._timeout = settings.integration_http_timeout_seconds
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            auth=(self.creds.auth_id, self.creds.auth_token),
        )

    def callback_url(self, kind: str, call_id: str) -> str:
        base = get_settings().public_base_url.rstrip("/")
        return (
            f"{base}/api/v1/integrations/plivo/webhooks/{self.integration_id}/{kind}?cid={call_id}"
        )

    async def create_call(self, request_: OutboundCallRequest) -> str:
        cid = str(request_.call_id)
        body = {
            "from": self.creds.phone_number.lstrip("+"),
            "to": request_.agent_number.lstrip("+"),
            "answer_url": self.callback_url("answer", cid),
            "answer_method": "POST",
            "ring_url": self.callback_url("ring", cid),
            "ring_method": "POST",
            "hangup_url": self.callback_url("hangup", cid),
            "hangup_method": "POST",
            "fallback_url": self.callback_url("fallback", cid),
            "fallback_method": "POST",
            "ring_timeout": 45,
        }
        try:
            async with self._http() as http:
                resp = await request(
                    http,
                    "POST",
                    f"{self._base}/Call/",
                    provider=NAME,
                    operation="create_call",
                    json=body,
                )
            raise_for_status(resp, provider=NAME)
            request_uuid = resp.json().get("request_uuid")
        except (ProviderError, ValueError) as exc:
            raise TelephonyError(
                f"plivo create_call failed: {type(exc).__name__}",
                code=f"plivo_{getattr(exc, 'code', type(exc).__name__)}",
            ) from exc
        if isinstance(request_uuid, list):
            request_uuid = request_uuid[0] if request_uuid else None
        if not request_uuid:
            raise TelephonyError("plivo returned no request_uuid")
        return str(request_uuid)[:128]

    async def end_call(self, provider_call_id: str) -> None:
        try:
            async with self._http() as http:
                resp = await request(
                    http,
                    "DELETE",
                    f"{self._base}/Call/{provider_call_id}/",
                    provider=NAME,
                    operation="hangup_call",
                )
                if resp.status_code == 404:
                    # Not answered yet: cancel the queued request instead.
                    resp = await request(
                        http,
                        "DELETE",
                        f"{self._base}/Request/{provider_call_id}/",
                        provider=NAME,
                        operation="cancel_request",
                    )
                if resp.status_code != 404:
                    raise_for_status(resp, provider=NAME)
        except ProviderError as exc:
            raise TelephonyError(
                f"plivo end_call failed: {exc.code}", code=f"plivo_{exc.code}"
            ) from exc

    async def start_stream(
        self, call_uuid: str, *, service_url: str, status_callback_url: str
    ) -> None:
        """Fork both tracks of the live call to our media WebSocket (listen-only, mu-law 8 kHz).
        Called once the customer answered. Raises TelephonyError when Plivo refuses."""
        body = {
            "service_url": service_url,
            "bidirectional": False,
            "audio_track": "both",
            "content_type": "audio/x-mulaw;rate=8000",
            "stream_timeout": 4 * 3600,
            "status_callback_url": status_callback_url,
            "status_callback_method": "POST",
        }
        try:
            async with self._http() as http:
                resp = await request(
                    http,
                    "POST",
                    f"{self._base}/Call/{call_uuid}/Stream/",
                    provider=NAME,
                    operation="start_stream",
                    json=body,
                )
            raise_for_status(resp, provider=NAME)
        except ProviderError as exc:
            raise TelephonyError(
                f"plivo start_stream failed: {exc.code}", code=f"plivo_stream_{exc.code}"
            ) from exc

    # Webhooks for Plivo are handled by app/integrations/webhooks.py (per-integration
    # signature). The generic mock-style webhook entry points are not used.
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> None:
        raise WebhookRejectedError("use the Plivo integration webhook endpoints")

    def parse_webhook(self, body: bytes) -> list[Any]:
        return []

    def parse_media_message(self, message: str) -> MediaFrame | MediaControl | None:
        return parse_media_message(message)

    async def test_connection(self) -> ConnectionReport:
        report = ConnectionReport()
        async with self._http() as http:
            try:
                resp = await request(
                    http,
                    "GET",
                    f"{self._base}/",
                    provider=NAME,
                    operation="get_account",
                    retries=1,
                )
                raise_for_status(resp, provider=NAME)
                info = resp.json()
                report.account_label = (
                    str(info.get("name") or info.get("account_type") or "")[:100] or None
                )
                report.add("account", "Auth ID and auth token are valid", True)
            except ProviderAuthError:
                report.add(
                    "account",
                    "Auth ID and auth token are valid",
                    False,
                    "Plivo rejected the Auth ID / auth token.",
                )
            except ProviderError as exc:
                report.add(
                    "account",
                    "Auth ID and auth token are valid",
                    False,
                    f"Plivo could not be reached or returned an error ({exc.code}).",
                )
            if report.ok:
                number = self.creds.phone_number.lstrip("+")
                try:
                    resp = await request(
                        http,
                        "GET",
                        f"{self._base}/Number/{number}/",
                        provider=NAME,
                        operation="get_number",
                        retries=1,
                    )
                    raise_for_status(resp, provider=NAME)
                    report.add("number", "Phone number belongs to this Plivo account", True)
                except ProviderRequestError:
                    report.add(
                        "number",
                        "Phone number belongs to this Plivo account",
                        False,
                        "This number is not rented on the Plivo account.",
                    )
                except ProviderError as exc:
                    report.add(
                        "number",
                        "Phone number belongs to this Plivo account",
                        False,
                        f"Could not verify the number ({exc.code}).",
                    )
        report.capabilities[Capability.PHONE_CALL.value] = report.ok
        report.capabilities[Capability.MEDIA_STREAM.value] = report.ok
        report.external_account_id = self.creds.auth_id
        return report


def mock_test_report() -> ConnectionReport:
    report = ConnectionReport(account_label="Mock phone provider")
    report.add("mock", "Mock mode: no request sent to Plivo; calls use the simulator", True)
    report.capabilities[Capability.PHONE_CALL.value] = True
    report.capabilities[Capability.MEDIA_STREAM.value] = True
    return report


def now() -> datetime:
    return datetime.now(UTC)
