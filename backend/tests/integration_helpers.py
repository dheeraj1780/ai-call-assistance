"""Helpers for the integration/conversation tests (no real provider is ever contacted: every
LIVE adapter runs through httpx.MockTransport)."""

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from httpx import AsyncClient

from app.integrations.providers import factory
from tests.conftest import Account

PUBLIC = "https://cc.example.com"

WA_VALUES = {
    "phone_number_id": "111222333",
    "business_account_id": "444555666",
    "access_token": "EAAG-secret-access-token-value",
    "app_secret": "meta-app-secret-value-123",
    "verify_token": "verify-token-abcdef123456",
}
PLIVO_VALUES = {
    "auth_id": "MAXXXXXXXXXXXXXXXXXX",
    "auth_token": "plivo-auth-token-secret-value",
    "phone_number": "+918000000000",
}
TEAMS_VALUES = {
    "tenant_id": "11111111-2222-3333-4444-555555555555",
    "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "client_secret": "teams-client-secret-value",
}


@dataclass
class FakeProvider:
    """Routes adapter HTTP calls to handlers and records every request."""

    routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = field(
        default_factory=list
    )
    requests: list[httpx.Request] = field(default_factory=list)

    def on(
        self,
        method: str,
        contains: str,
        response: Callable[[httpx.Request], httpx.Response] | httpx.Response,
    ) -> None:
        handler = response if callable(response) else (lambda _r, resp=response: resp)
        self.routes.insert(0, (method, contains, handler))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, contains, h in self.routes:
            if request.method == method and contains in str(request.url):
                return h(request)
        return httpx.Response(404, json={"error": {"code": "not_mocked"}})

    def install(self) -> "FakeProvider":
        factory.set_http_transport(httpx.MockTransport(self.handler))
        return self

    def calls(self, method: str, contains: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and contains in str(r.url)]


async def configure(
    client: AsyncClient,
    acct: Account,
    slug: str,
    values: dict[str, Any] | None = None,
    *,
    mode: str = "LIVE",
) -> dict[str, Any]:
    resp = await client.put(
        f"/api/v1/integrations/{slug}/config",
        headers=acct.headers,
        json={"mode": mode, "values": values or {}},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def validate_and_enable(
    client: AsyncClient, acct: Account, slug: str, *caps: str
) -> dict[str, Any]:
    resp = await client.post(f"/api/v1/integrations/{slug}/test", headers=acct.headers)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    for cap in caps:
        resp = await client.post(
            f"/api/v1/integrations/{slug}/capabilities/{cap}/enable", headers=acct.headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
    return body


def capability(detail: dict[str, Any], cap: str) -> dict[str, Any]:
    found: dict[str, Any] = next(c for c in detail["capabilities"] if c["capability"] == cap)
    return found


def wa_fake() -> FakeProvider:
    fake = FakeProvider()
    fake.on(
        "GET",
        "/111222333?",
        httpx.Response(
            200,
            json={
                "verified_name": "Acme",
                "display_phone_number": "+91 80000 00000",
                "id": "111222333",
            },
        ),
    )
    fake.on(
        "GET", "/444555666/phone_numbers", httpx.Response(200, json={"data": [{"id": "111222333"}]})
    )
    fake.on(
        "POST",
        "/111222333/messages",
        lambda r: httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "messages": [{"id": f"wamid.out.{len(r.content)}"}],
            },
        ),
    )
    return fake.install()


def wa_payload(
    *,
    wa_id: str,
    text: str,
    message_id: str,
    name: str = "Ravi",
    phone_number_id: str = "111222333",
) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "444555666",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "918000000000",
                                    "phone_number_id": phone_number_id,
                                },
                                "contacts": [{"profile": {"name": name}, "wa_id": wa_id}],
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


def wa_status_payload(*, wa_id: str, message_id: str, status: str) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "444555666",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "111222333"},
                                "statuses": [
                                    {
                                        "id": message_id,
                                        "status": status,
                                        "timestamp": str(int(time.time())),
                                        "recipient_id": wa_id,
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def wa_sign(body: bytes, secret: str = WA_VALUES["app_secret"]) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def integration_id(detail: dict[str, Any]) -> str:
    url: str = (
        detail["setup"].get("webhook_url")
        or detail["setup"].get("inbound_answer_url")
        or detail["setup"]["notification_url"]
    )
    parts = url.split("/")
    for i, part in enumerate(parts):
        if part == "webhooks":
            return parts[i + 1]
    raise AssertionError(url)
