"""Provider-neutral interfaces for communication integrations.

Business services depend only on these protocols and on the normalised conversation events in
``app/integrations/normalizer.py``. Concrete adapters (Teams, WhatsApp, Plivo, mocks) are built
by ``app/integrations/providers/factory.py`` from an integration record - services never
instantiate SDK/HTTP clients themselves (dependency injection; tests inject transports).

Error model: every adapter converts provider failures into ``ProviderError`` subclasses so
callers can degrade gracefully (a provider outage never corrupts CRM state).
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.integrations.normalizer import ConversationEvent

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    code = "provider_error"
    retry_after: int | None = None


class ProviderAuthError(ProviderError):
    """Credentials rejected (expired/revoked token, wrong secret)."""

    code = "provider_auth_failed"


class ProviderPermissionError(ProviderError):
    """Authenticated but missing a permission/scope."""

    code = "provider_permission_missing"

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = missing or []


class ProviderRateLimitError(ProviderError):
    code = "provider_rate_limited"


class ProviderUnavailableError(ProviderError):
    """Timeout, network error or 5xx."""

    code = "provider_unavailable"


class ProviderRequestError(ProviderError):
    """The provider rejected the request (4xx other than auth/permission/rate limit)."""

    code = "provider_request_rejected"

    def __init__(self, message: str, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class WebhookRejectedError(Exception):
    """Signature/verification failure. The reason is logged, never returned to the caller."""


@dataclass(frozen=True)
class CheckResult:
    key: str
    label: str
    ok: bool
    detail: str | None = None
    # Optional checks (e.g. calling permissions on a messaging-only setup) inform the
    # per-capability result but do not fail the connection test as a whole.
    required: bool = True


@dataclass
class ConnectionReport:
    checks: list[CheckResult] = field(default_factory=list)
    # Capability -> ok for capabilities this test validated.
    capabilities: dict[str, bool] = field(default_factory=dict)
    missing_permissions: list[str] = field(default_factory=list)
    account_label: str | None = None
    external_account_id: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks if c.required)

    def add(
        self, key: str, label: str, ok: bool, detail: str | None = None, *, required: bool = True
    ) -> bool:
        self.checks.append(CheckResult(key, label, ok, detail, required))
        return ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {
                    "key": c.key,
                    "label": c.label,
                    "ok": c.ok,
                    "detail": c.detail,
                    "required": c.required,
                }
                for c in self.checks
            ],
            "capabilities": self.capabilities,
            "missing_permissions": self.missing_permissions,
            "account_label": self.account_label,
        }


@dataclass(frozen=True)
class OutboundMessage:
    to: str  # provider address (WhatsApp wa_id / phone, Teams chat id)
    text: str
    client_message_id: str


@dataclass(frozen=True)
class SentMessage:
    external_message_id: str
    sent_at: datetime | None = None


class MessageProvider(Protocol):
    name: str

    async def send_text(self, message: OutboundMessage) -> SentMessage: ...


class ConnectionTester(Protocol):
    async def test_connection(self) -> ConnectionReport: ...


class WebhookProvider(Protocol):
    """Verifies and normalises provider webhooks for ONE integration (tenant already resolved
    from the integration route; the provider signature proves the sender)."""

    def verify(self, *, url: str, headers: Mapping[str, str], body: bytes) -> None: ...
    def parse(self, body: bytes) -> list[ConversationEvent]: ...


class ContactSyncProvider(Protocol):
    """Import/lookup of provider contacts. Declared for the architecture; no adapter
    implements it yet (CONTACT_SYNC capability is not offered)."""

    async def lookup(self, external_id: str) -> dict[str, str] | None: ...


# ---- shared HTTP helpers -----------------------------------------------------------------------


def raise_for_status(resp: httpx.Response, *, provider: str) -> None:
    """Map an HTTP error response to a ProviderError. Response bodies are not logged (they may
    contain personal data); only the status and a short provider error code are kept."""
    if resp.status_code < 400:
        return
    code: str | None = None
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            raw = err.get("code") or err.get("type")
            code = str(raw)[:64] if raw is not None else None
        elif isinstance(err, str):
            code = err[:64]
    except ValueError:
        pass
    if resp.status_code == 401:
        raise ProviderAuthError(f"{provider}: unauthorized")
    if resp.status_code == 403:
        raise ProviderPermissionError(f"{provider}: forbidden ({code or 'no code'})")
    if resp.status_code == 429:
        exc = ProviderRateLimitError(f"{provider}: rate limited")
        try:
            exc.retry_after = int(resp.headers.get("retry-after", "30"))
        except ValueError:
            exc.retry_after = 30
        raise exc
    if resp.status_code >= 500:
        raise ProviderUnavailableError(f"{provider}: status {resp.status_code}")
    raise ProviderRequestError(f"{provider}: status {resp.status_code}", provider_code=code)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    operation: str,
    retries: int = 0,
    **kwargs: Any,
) -> httpx.Response:
    """One provider HTTP call with timeout mapping, bounded retries (only callers doing
    idempotent requests pass ``retries``) and a structured log line without any payload."""
    attempt = 0
    while True:
        start = time.perf_counter()
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("provider_timeout", extra={"provider": provider, "op": operation})
            if attempt < retries:
                attempt += 1
                continue
            raise ProviderUnavailableError(f"{provider}: timeout") from exc
        except httpx.HTTPError as exc:
            logger.warning("provider_network_error", extra={"provider": provider, "op": operation})
            if attempt < retries:
                attempt += 1
                continue
            raise ProviderUnavailableError(f"{provider}: network error") from exc
        logger.info(
            "provider_request",
            extra={
                "provider": provider,
                "op": operation,
                "status": resp.status_code,
                "ms": int((time.perf_counter() - start) * 1000),
            },
        )
        if resp.status_code >= 500 and attempt < retries:
            attempt += 1
            continue
        return resp
