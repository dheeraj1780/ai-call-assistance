"""Pure adapter logic: signatures, payload normalisation, identifiers, XML, secret handling.

Plivo V3 vectors were produced with the official plivo-python SDK's signature code
(plivo/utils/signature_v3.py, v4.62.0) so the port is checked against the reference.
"""

import base64
import hashlib
import hmac
import json
import uuid
import xml.etree.ElementTree as ET

import pytest

from app.conversations.identity import normalize_email, normalize_phone, phone_variants
from app.integrations.domain import Capability, ConversationEventType, IntegrationMode, Provider
from app.integrations.normalizer import html_to_text
from app.integrations.providers import plivo, teams, whatsapp
from app.integrations.providers.base import ConnectionReport, WebhookRejectedError
from app.integrations.registry import SPECS, RequirementContext, blocking_missing, requirements
from app.telephony.provider import MediaControl, MediaFrame, Track, parse_json_media_message

# ---- identifiers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91 98765 43210", "919876543210"),
        ("0091-9876543210", "919876543210"),
        ("09876543210", "919876543210"),
        ("9876543210", "919876543210"),
        ("+1 (415) 555-0100", "14155550100"),
        ("12345", None),
        ("", None),
        (None, None),
        ("abc", None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw, default_country_code="91") == expected


def test_phone_variants_cover_national_formats() -> None:
    assert set(phone_variants("919876543210", "91")) == {
        "919876543210",
        "9876543210",
        "09876543210",
        "00919876543210",
    }


def test_normalize_email() -> None:
    assert normalize_email(" Ravi@ABC.in ") == "ravi@abc.in"
    assert normalize_email("not-an-email") is None


# ---- WhatsApp ------------------------------------------------------------------------------------

APP_SECRET = "meta-app-secret-value"


def wa_body(**value: object) -> bytes:
    base: dict[str, object] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": "111"},
    }
    base.update(value)
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "999", "changes": [{"field": "messages", "value": base}]}],
        }
    ).encode()


def test_whatsapp_signature() -> None:
    body = b'{"x":1}'
    good = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    whatsapp.verify_signature(APP_SECRET, {"x-hub-signature-256": good}, body)
    with pytest.raises(WebhookRejectedError):
        whatsapp.verify_signature(APP_SECRET, {"x-hub-signature-256": good}, body + b" ")
    with pytest.raises(WebhookRejectedError):
        whatsapp.verify_signature(APP_SECRET, {}, body)
    with pytest.raises(WebhookRejectedError):
        whatsapp.verify_signature("", {"x-hub-signature-256": good}, body)


def test_whatsapp_verify_challenge() -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "tok-1234567890abcdef",
        "hub.challenge": "42",
    }
    assert whatsapp.verify_challenge("tok-1234567890abcdef", params) == "42"
    with pytest.raises(WebhookRejectedError):
        whatsapp.verify_challenge("other-token-000000000", params)
    with pytest.raises(WebhookRejectedError):
        whatsapp.verify_challenge("tok-1234567890abcdef", {**params, "hub.mode": "unsubscribe"})


def test_whatsapp_parse_text_message() -> None:
    body = wa_body(
        contacts=[{"profile": {"name": "Ravi"}, "wa_id": "919876543210"}],
        messages=[
            {
                "from": "919876543210",
                "id": "wamid.A",
                "timestamp": "1790000000",
                "type": "text",
                "text": {"body": "Can you send the quotation?"},
            }
        ],
    )
    [event] = whatsapp.parse_webhook(body, expected_phone_number_id="111")
    assert event.type == ConversationEventType.MESSAGE_RECEIVED
    assert event.external_session_id == "919876543210"
    assert event.participant is not None
    assert event.participant.display_name == "Ravi"
    assert event.participant.phone == "919876543210"
    assert event.message is not None
    assert event.message.text == "Can you send the quotation?"
    assert event.message.external_message_id == "wamid.A"
    assert event.message.message_type == "TEXT"


def test_whatsapp_parse_media_and_unknown_types() -> None:
    body = wa_body(
        messages=[
            {"from": "91", "id": "m1", "type": "image", "image": {"caption": "our store"}},
            {"from": "91", "id": "m2", "type": "audio", "audio": {}},
            {"from": "91", "id": "m3", "type": "reaction", "reaction": {}},
        ]
    )
    events = whatsapp.parse_webhook(body, expected_phone_number_id="111")
    assert [e.message.text for e in events if e.message] == [
        "[Image] our store",
        "[Voice note - not transcribed]",
        "[Unsupported message type]",
    ]
    assert events[2].message is not None
    assert events[2].message.message_type == "UNSUPPORTED"


def test_whatsapp_ignores_other_phone_number_ids() -> None:
    body = wa_body(messages=[{"from": "91", "id": "m1", "type": "text", "text": {"body": "hi"}}])
    assert whatsapp.parse_webhook(body, expected_phone_number_id="222") == []


def test_whatsapp_status_updates() -> None:
    body = wa_body(
        statuses=[
            {
                "id": "wamid.X",
                "status": "delivered",
                "timestamp": "1790000000",
                "recipient_id": "91",
            },
            {
                "id": "wamid.Y",
                "status": "failed",
                "recipient_id": "91",
                "errors": [{"code": 131047, "title": "Re-engagement message"}],
            },
        ]
    )
    events = whatsapp.parse_webhook(body, expected_phone_number_id="111")
    assert [(e.data["status"], e.data["error_code"]) for e in events] == [
        ("DELIVERED", None),
        ("FAILED", "131047"),
    ]
    assert all(e.type == ConversationEventType.MESSAGE_STATUS_UPDATED for e in events)


def test_whatsapp_malformed_payload_rejected() -> None:
    with pytest.raises(WebhookRejectedError):
        whatsapp.parse_webhook(b"not json", expected_phone_number_id="111")


# ---- Plivo -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "uri", "nonce", "token", "params", "expected"),
    [
        (
            "POST",
            "https://cc.example.com/api/v1/integrations/plivo/webhooks/abc/hangup?cid=123",
            "nonce1",
            "TOKEN123",
            {"CallUUID": "uuid-1", "CallStatus": "completed", "Duration": "42"},
            "TDwd11keExKf0nWUO37n/PyVrAoYK5cLkMJJM4vxBZc=",
        ),
        (
            "POST",
            "https://cc.example.com/api/v1/x",
            "n2",
            "tok",
            {},
            "nUWYpodabnjfGwC6clMFZ9jxkB61u3mmfhsKKNRQhxM=",
        ),
        (
            "POST",
            "https://cc.example.com/api/v1/x?b=2&a=1",
            "n3",
            "tok",
            {"Z": "z", "A": ["2", "1"]},
            "9TfZ19GgKEJcD0xCvymqOwldOG+DtnsuWFDI1MLp7D0=",
        ),
        (
            "GET",
            "https://cc.example.com/api/v1/x?b=2&a=1",
            "n4",
            "tok",
            {"c": "3"},
            "QlsHppEJAs0E19NhdY5A9oWlrwiVQNkFwzdCLG7+iCc=",
        ),
    ],
)
def test_plivo_signature_matches_official_sdk(
    method: str, uri: str, nonce: str, token: str, params: dict[str, str | list[str]], expected: str
) -> None:
    assert plivo.compute_signature_v3(token, uri, nonce, method, params) == expected


def test_plivo_verify_signature() -> None:
    uri = "https://cc.example.com/cb?cid=1"
    params: dict[str, list[str] | str] = {"CallUUID": "u"}
    sig = plivo.compute_signature_v3("tok", uri, "n", "POST", params)
    headers = {"x-plivo-signature-v3": f"old-rotated-sig,{sig}", "x-plivo-signature-v3-nonce": "n"}
    plivo.verify_signature_v3("tok", uri=uri, method="POST", headers=headers, params=params)
    with pytest.raises(WebhookRejectedError):
        plivo.verify_signature_v3("other", uri=uri, method="POST", headers=headers, params=params)
    with pytest.raises(WebhookRejectedError):
        plivo.verify_signature_v3(
            "tok", uri=uri, method="POST", headers=headers, params={"CallUUID": "tampered"}
        )
    with pytest.raises(WebhookRejectedError):
        plivo.verify_signature_v3("tok", uri=uri, method="POST", headers={}, params=params)


def test_plivo_answer_xml_streams_both_tracks_listen_only() -> None:
    xml = plivo.answer_xml(
        customer_number="+919876543210",
        caller_id="+918000000000",
        stream_url="wss://cc.example.com/media?token=a&leg=agent",
        stream_status_url="https://cc.example.com/s?cid=1",
        dial_action_url="https://cc.example.com/a?cid=1",
        dial_callback_url="https://cc.example.com/d?cid=1",
    )
    root = ET.fromstring(  # noqa: S314 - our own generated XML
        xml
    )
    stream = root.find("Stream")
    assert stream is not None
    assert stream.text == "wss://cc.example.com/media?token=a&leg=agent"  # escaped in XML
    assert stream.get("bidirectional") == "false"  # the AI never speaks
    assert stream.get("audioTrack") == "both"
    assert stream.get("keepCallAlive") == "false"
    dial = root.find("Dial")
    assert dial is not None
    assert dial.get("callerId") == "+918000000000"
    number = dial.find("Number")
    assert number is not None
    assert number.text == "+919876543210"
    no_stream = ET.fromstring(  # noqa: S314 - our own generated XML
        plivo.answer_xml(
            customer_number="+91",
            caller_id="+91",
            stream_url=None,
            stream_status_url="s",
            dial_action_url="a",
            dial_callback_url="d",
        )
    )
    assert no_stream.find("Stream") is None


@pytest.mark.parametrize(
    ("kind", "params", "state"),
    [
        ("ring", {"CallUUID": "u"}, "RINGING"),
        ("dial", {"CallUUID": "u", "DialAction": "answer"}, "CONNECTED"),
        ("dial", {"CallUUID": "u", "DialAction": "hangup"}, None),
        ("dial_action", {"CallUUID": "u", "DialStatus": "no-answer"}, "NO_ANSWER"),
        ("dial_action", {"CallUUID": "u", "DialStatus": "completed"}, None),
        ("hangup", {"CallUUID": "u", "CallStatus": "completed", "Duration": "61"}, "COMPLETED"),
        ("hangup", {"CallUUID": "u", "CallStatus": "busy"}, "NO_ANSWER"),
        ("hangup", {"CallUUID": "u", "CallStatus": "failed", "HangupCause": "X"}, "FAILED"),
    ],
)
def test_plivo_callbacks(kind: str, params: dict[str, str], state: str | None) -> None:
    cb = plivo.parse_callback(kind, dict(params))
    assert (cb.state.value if cb.state else None) == state
    assert cb.call_uuid == "u"


def test_plivo_callback_requires_call_uuid() -> None:
    with pytest.raises(WebhookRejectedError):
        plivo.parse_callback("hangup", {})


def test_plivo_media_messages() -> None:
    start = plivo.parse_media_message(
        json.dumps(
            {
                "event": "start",
                "start": {"mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}},
            }
        )
    )
    assert start == MediaControl(kind="start", encoding="mulaw", sample_rate=8000)
    payload = base64.b64encode(b"\x00\x01").decode()
    frame = plivo.parse_media_message(
        json.dumps(
            {
                "event": "media",
                "sequenceNumber": 3,
                "media": {"track": "outbound", "payload": payload},
            }
        )
    )
    assert frame == MediaFrame(track=Track.CUSTOMER, sequence=3, audio=b"\x00\x01")
    swapped = plivo.parse_media_message(
        json.dumps({"event": "media", "media": {"track": "outbound", "payload": payload}}),
        swap_tracks=True,
    )
    assert isinstance(swapped, MediaFrame)
    assert swapped.track == Track.AGENT
    assert plivo.parse_media_message("garbage") is None
    assert (
        plivo.parse_media_message(json.dumps({"event": "media", "media": {"track": "x"}})) is None
    )


def test_json_media_format() -> None:
    start = parse_json_media_message(
        json.dumps({"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}})
    )
    assert start == MediaControl(kind="start", encoding="linear16", sample_rate=16000)
    assert parse_json_media_message(
        json.dumps({"event": "start", "format": {"encoding": "evil"}})
    ) == MediaControl(kind="start")
    assert parse_json_media_message(json.dumps({"event": "stop"})) == MediaControl(kind="stop")


# ---- Teams ---------------------------------------------------------------------------------------


def _jwt(claims: dict[str, object]) -> str:
    def enc(d: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def test_teams_scope_and_role_claims() -> None:
    assert teams.granted_scopes(_jwt({"scp": "User.Read Chat.Read"})) == {"User.Read", "Chat.Read"}
    assert teams.missing_messaging_scopes({"User.Read", "Chat.Read"}) == ["ChatMessage.Send"]
    assert teams.granted_roles(_jwt({"roles": ["Calls.AccessMedia.All"]})) == {
        "Calls.AccessMedia.All"
    }
    assert teams.granted_roles("not-a-jwt") == set()


def test_teams_html_normalisation() -> None:
    assert html_to_text("<p>Hi&nbsp;there</p><p>Need <b>100</b> units<br>by Friday</p>") == (
        "Hi there\nNeed 100 units\nby Friday"
    )


def test_teams_message_normalisation() -> None:
    msg = {
        "id": "1612289765949",
        "messageType": "message",
        "createdDateTime": "2026-09-24T10:00:00Z",
        "from": {"user": {"id": "customer-aad", "displayName": "Ravi"}},
        "body": {"contentType": "html", "content": "<p>Send the quotation</p>"},
    }
    event = teams.normalize_message(msg, chat_id="19:chat", our_user_id="me-aad")
    assert event is not None
    assert event.type == ConversationEventType.MESSAGE_RECEIVED
    assert event.message is not None
    assert event.message.text == "Send the quotation"
    ours = teams.normalize_message(
        {**msg, "from": {"user": {"id": "me-aad"}}}, chat_id="19:chat", our_user_id="me-aad"
    )
    assert ours is not None
    assert ours.type == ConversationEventType.MESSAGE_SENT
    assert (
        teams.normalize_message(
            {**msg, "messageType": "systemEventMessage"}, chat_id="c", our_user_id="x"
        )
        is None
    )


def test_teams_notification_client_state_is_verified() -> None:
    iid = uuid.uuid4()
    good = teams.client_state_for(iid)
    body = json.dumps(
        {
            "value": [
                {
                    "subscriptionId": "sub-1",
                    "changeType": "created",
                    "clientState": good,
                    "resource": "chats('19:abc@thread.v2')/messages('1612')",
                }
            ]
        }
    ).encode()
    [n] = teams.parse_notifications(body, iid)
    assert (n.chat_id, n.message_id, n.subscription_id) == ("19:abc@thread.v2", "1612", "sub-1")
    with pytest.raises(WebhookRejectedError):
        teams.parse_notifications(body, uuid.uuid4())  # another integration's clientState


def test_gateway_signature() -> None:
    body = b'{"a":1}'
    headers = {k.lower(): v for k, v in teams.sign_gateway("s" * 32, body).items()}
    teams.verify_gateway_signature("s" * 32, headers, body)
    with pytest.raises(WebhookRejectedError):
        teams.verify_gateway_signature("t" * 32, headers, body)
    stale = {k.lower(): v for k, v in teams.sign_gateway("s" * 32, body, timestamp=1).items()}
    with pytest.raises(WebhookRejectedError):
        teams.verify_gateway_signature("s" * 32, stale, body)


# ---- registry / reports ------------------------------------------------------------------------


def test_test_report_optional_checks() -> None:
    r = ConnectionReport()
    r.add("creds", "Credentials", True)
    r.add("calling", "Calling", False, required=False)
    assert r.ok
    assert r.as_dict()["checks"][1]["required"] is False


def test_whatsapp_voice_is_not_available() -> None:
    cap = SPECS[whatsapp_provider()].capability(Capability.WHATSAPP_VOICE_CALL)
    assert cap is not None
    assert not cap.available
    assert cap.unavailable_reason


def whatsapp_provider() -> "Provider":
    return Provider.WHATSAPP


def test_mock_mode_has_no_requirements(settings_stub: object) -> None:
    spec = SPECS[whatsapp_provider()]
    reqs = requirements(
        spec,
        RequirementContext(IntegrationMode.MOCK, {}, set(), settings_stub, None),  # type: ignore[arg-type]
    )
    assert [r.key for r in reqs] == ["mock_mode"]
    live = requirements(
        spec,
        RequirementContext(IntegrationMode.LIVE, {}, set(), settings_stub, None),  # type: ignore[arg-type]
    )
    missing = {r.key for r in blocking_missing(live, Capability.MESSAGE)}
    assert "field:access_token" in missing
    assert "public_url" in missing
    assert "webhook_verified" not in missing  # verified by Meta only after enabling


@pytest.fixture
def settings_stub() -> object:
    class S:
        public_base_url = "http://localhost:8000"
        stt_provider = "mock"
        microsoft_client_id = None
        microsoft_client_secret = None
        teams_media_gateway_url = None
        teams_media_gateway_secret = None

    return S()
