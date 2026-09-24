"""Developer controls: simulate provider traffic for MOCK-mode integrations.

The simulators build REAL provider-shaped payloads (a WhatsApp Cloud API webhook body, a
Microsoft Graph chatMessage, Teams media-gateway events and audio frames) and push them through
the SAME parsing, normalisation, conversation, AI and timeline code as live traffic. Only the
transport/signature step is skipped because a mock integration has no provider secret.

Available only when SIMULATION_ENABLED=true, outside production, for integrations in MOCK mode.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.config import get_settings
from app.common.db import get_db_session
from app.common.errors import AppError, NotFoundError
from app.contacts.repository import ContactRepository
from app.conversations.identity import normalize_phone
from app.integrations import service as integrations
from app.integrations.domain import IntegrationMode, Provider
from app.integrations.providers import teams as teams_adapter
from app.integrations.providers import whatsapp as whatsapp_adapter
from app.integrations.webhooks import GatewayEvent, handle_gateway_event, process_message_events
from app.live import session as live
from app.telephony.provider import parse_json_media_message
from app.telephony.service import MediaIngest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/dev/simulate", tags=["developer"])

MOCK_PHONE_NUMBER_ID = "100000000000001"
MOCK_WABA_ID = "200000000000001"


def _require_simulation() -> None:
    s = get_settings()
    if not s.simulation_enabled or s.is_production:
        raise AppError("Simulation is not available", code="simulation_disabled").with_status(404)


async def _mock_record(session: AsyncSession, principal: Principal, provider: Provider) -> Any:
    record = await integrations.get_record(session, principal.company_id, provider)
    if record is None or record.mode != IntegrationMode.MOCK:
        raise AppError(
            "Put this integration in Mock mode (Settings > Integrations) to simulate traffic.",
            code="simulation_requires_mock_mode",
        ).with_status(409)
    return record


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class SimulateMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Text
    contact_id: uuid.UUID | None = None
    # Sender phone (WhatsApp) when not simulating an existing contact.
    phone: Annotated[str, StringConstraints(max_length=20)] | None = None
    sender_name: Annotated[str, StringConstraints(max_length=100)] | None = None
    # Teams: which linked mock chat the message arrives in.
    chat_id: Annotated[str, StringConstraints(max_length=256)] | None = None


def whatsapp_payload(*, wa_id: str, name: str | None, text: str, message_id: str) -> bytes:
    """A Cloud API 'messages' webhook body exactly as Meta documents it."""
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": MOCK_WABA_ID,
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550000000",
                                    "phone_number_id": MOCK_PHONE_NUMBER_ID,
                                },
                                "contacts": [
                                    {"profile": {"name": name or "Customer"}, "wa_id": wa_id}
                                ],
                                "messages": [
                                    {
                                        "from": wa_id,
                                        "id": message_id,
                                        "timestamp": str(int(time.time())),
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


@router.post("/whatsapp-message")
async def simulate_whatsapp_message(
    body: SimulateMessageIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_simulation()
    record = await _mock_record(session, principal, Provider.WHATSAPP)
    phone = body.phone
    name = body.sender_name
    if body.contact_id is not None:
        contact = await ContactRepository(session).get(principal.company_id, body.contact_id)
        if contact is None:
            raise NotFoundError("Contact not found")
        phone = contact.phone
        name = name or contact.name
    wa_id = normalize_phone(phone)
    if not wa_id:
        raise AppError("A valid phone number is needed", code="missing_phone").with_status(422)
    payload = whatsapp_payload(
        wa_id=wa_id, name=name, text=body.text, message_id=f"wamid.sim.{uuid.uuid4().hex}"
    )
    events = whatsapp_adapter.parse_webhook(payload, expected_phone_number_id=MOCK_PHONE_NUMBER_ID)
    counts = await process_message_events(session, record, events)
    return {"provider": "mock", "events": len(events), **counts}


@router.post("/teams-message")
async def simulate_teams_message(
    body: SimulateMessageIn,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_simulation()
    record = await _mock_record(session, principal, Provider.MICROSOFT_TEAMS)
    chat_id = body.chat_id or teams_adapter.MOCK_CHATS[0].id
    graph_message: dict[str, Any] = {
        "id": str(int(time.time() * 1000)),
        "messageType": "message",
        "createdDateTime": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deletedDateTime": None,
        "chatId": chat_id,
        "from": {
            "user": {
                "id": "11111111-2222-3333-4444-555555555555",
                "displayName": body.sender_name or "Customer (mock)",
                "userIdentityType": "aadUser",
            }
        },
        "body": {"contentType": "html", "content": f"<p>{body.text}</p>"},
        "attachments": [],
    }
    event = teams_adapter.normalize_message(
        graph_message, chat_id=chat_id, our_user_id=teams_adapter.MOCK_USER_ID
    )
    events = [event] if event else []
    counts = await process_message_events(session, record, events, owner_user_id=principal.user_id)
    return {"provider": "mock", "events": len(events), **counts}


# ---- Teams call simulation (used by POST /calls/{id}/simulate for teams-mock calls) ----------

running: dict[uuid.UUID, asyncio.Task[None]] = {}


def _frame(track: str, seq: int, text: str) -> str:
    return json.dumps(
        {
            "event": "media",
            "track": track,
            "seq": seq,
            "payload": base64.b64encode(text.encode()).decode(),
        }
    )


async def run_teams_call(
    call_id: uuid.UUID,
    gateway_call_id: str,
    *,
    persistence: str,
    script: list[tuple[str, str]],
    delay: float = 0.0,
    complete: bool = True,
) -> None:
    """Plays a Teams meeting through the gateway-event and media paths used by the real
    teams-media-gateway (MOCKED media: the mock STT reads each frame as text)."""
    from app.telephony.simulator import _processed, _wait_for_segment

    n = 0

    async def gw(
        state: str | None = None, recording: str | None = None, media: str | None = None
    ) -> None:
        nonlocal n
        n += 1
        await handle_gateway_event(
            "teams-mock",
            GatewayEvent(
                event_id=f"sim-{call_id}-{n}",
                call_id=call_id,
                gateway_call_id=gateway_call_id,
                state=state,
                recording_status=recording,
                media_status=media,
            ),
        )

    try:
        await gw("ESTABLISHING")
        await asyncio.sleep(delay)
        if persistence == "RECORDING_DECLARED":
            # The real gateway calls updateRecordingStatus and only then streams audio.
            await gw(recording="RECORDING_CONFIRMED")
        await gw("ESTABLISHED")
        await gw(media="AVAILABLE")  # the real gateway reports this once its media socket is up
        ingest = MediaIngest(call_id)
        await ingest.handle(
            parse_json_media_message(
                json.dumps(
                    {"event": "start", "format": {"encoding": "linear16", "sample_rate": 16000}}
                )
            )
        )
        for seq, (speaker, text) in enumerate(script, start=1):
            await asyncio.sleep(delay)
            before = _processed(call_id)
            await ingest.handle(parse_json_media_message(_frame(speaker, seq, text)))
            await _wait_for_segment(call_id, before)
        await asyncio.sleep(max(delay, 0.05))
        await ingest.handle(parse_json_media_message(json.dumps({"event": "stop"})))
        if complete:
            await gw("TERMINATED")
    except Exception:
        logger.exception("teams_simulation_failed", extra={"call_id": str(call_id)})
    finally:
        running.pop(call_id, None)
        if live.get_session(call_id) is None:
            pass


def start_teams_call(
    call_id: uuid.UUID,
    gateway_call_id: str,
    *,
    persistence: str,
    script: list[tuple[str, str]],
    delay: float,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        run_teams_call(
            call_id, gateway_call_id, persistence=persistence, script=script, delay=delay
        )
    )
    running[call_id] = task
    return task
