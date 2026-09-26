"""Integration configuration use cases (admin) and the runtime "is this capability usable?"
check used by the conversation and calling services.

Status model
- integration: NOT_CONFIGURED -> CONFIGURED -> VALIDATED -> ENABLED, or ERROR
- capability:  NOT_AVAILABLE | NOT_CONFIGURED | CONFIGURED | VALIDATED | ENABLED | ERROR
A connection test is valid only for the configuration version it ran against. Changing any
credential clears the test and disables all capabilities (they must be re-validated).
Secrets are write-only: they are encrypted before storage and never returned.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal
from app.common.config import get_settings
from app.common.db import TenantContext, get_session_factory, set_tenant_context
from app.common.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.integrations.domain import (
    SLUG_OF,
    Capability,
    CapabilityState,
    IntegrationMode,
    IntegrationStatus,
    Provider,
)
from app.integrations.models import (
    Integration,
    IntegrationRoute,
    IntegrationSubscription,
    IntegrationUserConnection,
)
from app.integrations.providers import factory
from app.integrations.providers import plivo as plivo_adapter
from app.integrations.providers import teams as teams_adapter
from app.integrations.providers.base import ConnectionReport, ProviderError
from app.integrations.registry import (
    SPECS,
    ProviderSpec,
    Requirement,
    RequirementContext,
    blocking_missing,
    requirements,
)
from app.integrations.secrets import get_secret_store

logger = logging.getLogger(__name__)


class IntegrationNotReadyError(AppError):
    status_code = 409
    code = "integration_not_ready"
    message = "This channel is not set up yet."


class IntegrationValidationError(AppError):
    status_code = 422
    code = "invalid_integration_config"


@dataclass
class CapabilityView:
    capability: Capability
    label: str
    description: str
    channel: str
    state: CapabilityState
    enabled: bool
    available: bool
    reasons: list[str]


@dataclass
class IntegrationView:
    spec: ProviderSpec
    record: Integration | None
    status: IntegrationStatus
    capabilities: list[CapabilityView]
    requirements: list[Requirement]

    @property
    def mode(self) -> IntegrationMode:
        return IntegrationMode(self.record.mode) if self.record else IntegrationMode.LIVE


def _require_admin(principal: Principal) -> None:
    if not principal.is_admin:
        raise ForbiddenError("Only owners and admins can manage integrations")


def secrets_of(record: Integration) -> dict[str, str]:
    return get_secret_store().open(record.secrets_ciphertext)


async def get_record(
    session: AsyncSession, company_id: uuid.UUID, provider: Provider
) -> Integration | None:
    row: Integration | None = await session.scalar(
        select(Integration).where(
            Integration.company_id == company_id, Integration.provider == provider.value
        )
    )
    return row


def _current_test(record: Integration | None) -> dict[str, Any] | None:
    if record is None or not record.last_test:
        return None
    if record.last_test.get("config_version") != record.config_version:
        return None
    return record.last_test


def _required_fields_missing(
    spec: ProviderSpec, record: Integration | None, cap: Capability
) -> list[str]:
    if record is None:
        return [f.label for f in spec.fields if f.required]
    if record.mode == IntegrationMode.MOCK:
        return []
    keys = set(record.secret_keys)
    return [
        f.label
        for f in spec.fields
        if f.required
        and (not f.capabilities or cap in f.capabilities)
        and not (f.key in keys or record.config.get(f.key))
    ]


def build_view(
    spec: ProviderSpec, record: Integration | None, *, user_connected: bool | None = None
) -> IntegrationView:
    settings = get_settings()
    test = _current_test(record)
    reqs = requirements(
        spec,
        RequirementContext(
            mode=IntegrationMode(record.mode) if record else IntegrationMode.LIVE,
            config=dict(record.config) if record else {},
            secret_keys=set(record.secret_keys) if record else set(),
            settings=settings,
            last_test=test,
            user_connected=user_connected,
        ),
    )
    enabled = set(record.enabled_capabilities) if record else set()
    caps: list[CapabilityView] = []
    for c in spec.capabilities:
        reasons: list[str] = []
        if not c.available:
            state = CapabilityState.NOT_AVAILABLE
            reasons = [c.unavailable_reason or "Not available"]
        else:
            missing_fields = _required_fields_missing(spec, record, c.capability)
            blockers = (
                blocking_missing(reqs, c.capability)
                if record and record.mode == IntegrationMode.LIVE
                else []
            )
            tested = (test or {}).get("capabilities", {}).get(c.capability.value)
            if record is None or missing_fields:
                state = CapabilityState.NOT_CONFIGURED
                reasons = [f"Missing: {m}" for m in missing_fields] or ["Not configured"]
            elif record.error_code and c.capability.value in enabled:
                state = CapabilityState.ERROR
                reasons = [f"Provider error: {record.error_code}. Run Test Connection again."]
            elif tested is False:
                state = CapabilityState.ERROR
                reasons = ["The last connection test failed for this capability."]
            elif tested is True and not blockers:
                state = (
                    CapabilityState.ENABLED
                    if c.capability.value in enabled
                    else CapabilityState.VALIDATED
                )
            elif blockers:
                state = CapabilityState.NOT_CONFIGURED
                reasons = [r.label for r in blockers]
            else:
                state = CapabilityState.CONFIGURED
                reasons = ["Run Test Connection to validate."]
        caps.append(
            CapabilityView(
                c.capability,
                c.label,
                c.description,
                c.channel.value,
                state,
                state == CapabilityState.ENABLED,
                c.available,
                reasons,
            )
        )
    status = _status(record, caps, test)
    return IntegrationView(spec, record, status, caps, reqs)


def _status(
    record: Integration | None, caps: list[CapabilityView], test: dict[str, Any] | None
) -> IntegrationStatus:
    usable = [c for c in caps if c.available]
    if record is None or all(c.state == CapabilityState.NOT_CONFIGURED for c in usable):
        return IntegrationStatus.NOT_CONFIGURED
    if record.error_code or (test is not None and not test.get("ok")):
        return IntegrationStatus.ERROR
    if any(c.state == CapabilityState.ENABLED for c in usable):
        return IntegrationStatus.ENABLED
    if test is not None and test.get("ok"):
        return IntegrationStatus.VALIDATED
    return IntegrationStatus.CONFIGURED


async def user_connected(session: AsyncSession, principal: Principal, provider: Provider) -> bool:
    row = await session.scalar(
        select(IntegrationUserConnection.id).where(
            IntegrationUserConnection.company_id == principal.company_id,
            IntegrationUserConnection.user_id == principal.user_id,
            IntegrationUserConnection.provider == provider.value,
            IntegrationUserConnection.status == "ACTIVE",
        )
    )
    return row is not None


async def view_for(
    session: AsyncSession, principal: Principal, provider: Provider
) -> IntegrationView:
    record = await get_record(session, principal.company_id, provider)
    connected = (
        await user_connected(session, principal, provider)
        if provider in (Provider.MICROSOFT_TEAMS, Provider.GOOGLE_MEET)
        else None
    )
    if record is not None and record.mode == IntegrationMode.MOCK:
        connected = True  # mock mode needs no per-user Microsoft sign-in
    return build_view(SPECS[provider], record, user_connected=connected)


async def list_views(session: AsyncSession, principal: Principal) -> list[IntegrationView]:
    return [await view_for(session, principal, p) for p in SPECS]


# ---- configure ---------------------------------------------------------------------------------


def _validate_values(spec: ProviderSpec, values: dict[str, str | None]) -> dict[str, str | None]:
    clean: dict[str, str | None] = {}
    for key, raw in values.items():
        f = spec.field(key)
        if f is None:
            raise IntegrationValidationError(f"Unknown field: {key}", details=[{"field": key}])
        value = raw.strip() if isinstance(raw, str) else None
        if value == "":
            value = None
        if value is not None:
            if len(value) > f.max_length:
                raise IntegrationValidationError(f"{f.label} is too long", details=[{"field": key}])
            if f.pattern and not re.fullmatch(f.pattern, value):
                raise IntegrationValidationError(
                    f"{f.label} has an invalid format", details=[{"field": key}]
                )
            if f.options and value not in {o[0] for o in f.options}:
                raise IntegrationValidationError(
                    f"{f.label} has an invalid value", details=[{"field": key}]
                )
        clean[key] = value
    return clean


async def configure(
    session: AsyncSession,
    principal: Principal,
    provider: Provider,
    *,
    mode: IntegrationMode,
    values: dict[str, str | None],
    clear_secrets: list[str],
    ip: str | None,
) -> IntegrationView:
    _require_admin(principal)
    settings = get_settings()
    spec = SPECS[provider]
    if mode == IntegrationMode.MOCK and not settings.integration_mock_allowed:
        raise IntegrationValidationError("Mock mode is not allowed in production")
    clean = _validate_values(spec, values)
    record = await get_record(session, principal.company_id, provider)
    if record is None:
        record = Integration(
            id=uuid.uuid4(),
            company_id=principal.company_id,
            provider=provider.value,
            mode=mode.value,
            config={},
            secret_keys=[],
            enabled_capabilities=[],
            config_version=1,
        )
        session.add(record)
    store = get_secret_store()
    secrets = store.open(record.secrets_ciphertext)
    config = dict(record.config)
    changed = record.mode != mode.value
    for key, value in clean.items():
        f = spec.field(key)
        if f is None:  # pragma: no cover - validated above
            continue
        if f.kind == "secret":
            if value is not None and secrets.get(key) != value:
                secrets[key] = value
                changed = True
        elif config.get(key) != value:
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
            changed = True
    for key in clear_secrets:
        if spec.field(key) is None:
            raise IntegrationValidationError(f"Unknown field: {key}")
        if secrets.pop(key, None) is not None:
            changed = True
    for f in spec.fields:
        if f.kind == "select" and f.default and not config.get(f.key):
            config[f.key] = f.default
    record.mode = mode.value
    record.config = config
    record.secret_keys = sorted(secrets)
    record.secrets_ciphertext = store.seal(secrets) if secrets else None
    if changed:
        record.config_version = (record.config_version or 0) + 1
        record.enabled_capabilities = []
        record.last_test = None
        record.last_tested_at = None
        record.error_code = None
        record.error_at = None
        config.pop("webhook_verified_at", None)
        record.config = config
    await session.flush()

    external = (
        str(config.get(spec.external_account_field) or "") or None
        if spec.external_account_field and mode == IntegrationMode.LIVE
        else None
    )
    stmt = insert(IntegrationRoute).values(
        integration_id=record.id,
        company_id=principal.company_id,
        provider=provider.value,
        external_account_id=external,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["integration_id"], set_={"external_account_id": external}
    )
    try:
        async with session.begin_nested():
            await session.execute(stmt)
    except IntegrityError:
        raise ConflictError(
            "This provider account is already connected to another workspace.",
            code="integration_account_in_use",
        ) from None
    audit.record(
        session,
        "integration.configured",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration",
        entity_id=record.id,
        ip=ip,
        # Field names only - never values.
        details={"provider": provider.value, "mode": mode.value, "fields": sorted(clean)},
    )
    await session.commit()
    return await view_for(session, principal, provider)


async def _run_test(record: Integration, secrets: dict[str, str]) -> ConnectionReport:
    provider = Provider(record.provider)
    if provider == Provider.WHATSAPP:
        return await factory.whatsapp_client(record, secrets).test_connection()
    if provider == Provider.PLIVO:
        if record.mode == IntegrationMode.MOCK:
            return plivo_adapter.mock_test_report()
        live = factory.plivo_provider(record, secrets, stream_enabled=False)
        if not isinstance(live, plivo_adapter.PlivoTelephonyProvider):  # pragma: no cover
            raise factory.CredentialsMissingError("plivo")
        return await live.test_connection()
    if record.mode == IntegrationMode.MOCK:
        return teams_adapter.mock_test_report()
    return await factory.graph_client(record, secrets).test_connection(
        gateway=factory.gateway_client()
    )


async def run_test(
    session: AsyncSession, principal: Principal, provider: Provider, *, ip: str | None
) -> tuple[IntegrationView, ConnectionReport]:
    _require_admin(principal)
    record = await get_record(session, principal.company_id, provider)
    if record is None:
        raise NotFoundError("Configure this integration first")
    try:
        if provider == Provider.GOOGLE_MEET:
            from app.integrations import google_meet_service

            report = await google_meet_service.test_connection(session, principal, record)
        else:
            report = await _run_test(record, secrets_of(record))
    except factory.CredentialsMissingError as exc:
        report = ConnectionReport()
        report.add("credentials", "Required credentials are present", False, f"Missing: {exc}")
    except ProviderError as exc:
        report = ConnectionReport()
        report.add("provider", "Provider reachable", False, f"Provider error ({exc.code}).")
    result = report.as_dict()
    result["config_version"] = record.config_version
    record.last_test = result
    record.last_tested_at = datetime.now(UTC)
    if report.ok:
        record.error_code = None
        record.error_at = None
    # Drop enabled capabilities that no longer pass.
    record.enabled_capabilities = [
        c for c in record.enabled_capabilities if report.capabilities.get(c)
    ]
    if report.external_account_id and SPECS[provider].external_account_field:
        pass  # the route already carries the configured account id
    audit.record(
        session,
        "integration.tested",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration",
        entity_id=record.id,
        ip=ip,
        details={"provider": provider.value, "ok": report.ok},
    )
    await session.commit()
    return await view_for(session, principal, provider), report


async def set_capability(
    session: AsyncSession,
    principal: Principal,
    provider: Provider,
    capability: Capability,
    *,
    enabled: bool,
    ip: str | None,
) -> IntegrationView:
    _require_admin(principal)
    spec = SPECS[provider]
    if spec.capability(capability) is None:
        raise NotFoundError("This provider does not offer that capability")
    record = await get_record(session, principal.company_id, provider)
    if record is None:
        raise NotFoundError("Configure this integration first")
    current = set(record.enabled_capabilities)
    if enabled:
        view = build_view(spec, record, user_connected=True)
        cap = next(c for c in view.capabilities if c.capability == capability)
        if cap.state not in (CapabilityState.VALIDATED, CapabilityState.ENABLED):
            raise IntegrationNotReadyError(
                "This capability cannot be enabled yet: " + "; ".join(cap.reasons),
                details={"state": cap.state.value, "reasons": cap.reasons},
            )
        current.add(capability.value)
    else:
        current.discard(capability.value)
    record.enabled_capabilities = sorted(current)
    audit.record(
        session,
        "integration.capability_enabled" if enabled else "integration.capability_disabled",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration",
        entity_id=record.id,
        ip=ip,
        details={"provider": provider.value, "capability": capability.value},
    )
    await session.commit()
    return await view_for(session, principal, provider)


async def disconnect(
    session: AsyncSession, principal: Principal, provider: Provider, *, ip: str | None
) -> None:
    """Remove credentials, routing, user connections and subscriptions. Conversation history
    stays (it belongs to the CRM, subject to retention)."""
    _require_admin(principal)
    record = await get_record(session, principal.company_id, provider)
    if record is None:
        return
    if provider == Provider.MICROSOFT_TEAMS and record.mode == IntegrationMode.LIVE:
        from app.integrations import teams_service

        await teams_service.delete_all_subscriptions(session, principal.company_id, record)
    await session.execute(
        delete(IntegrationRoute).where(IntegrationRoute.integration_id == record.id)
    )
    await session.execute(
        delete(IntegrationSubscription).where(
            IntegrationSubscription.company_id == principal.company_id,
            IntegrationSubscription.integration_id == record.id,
        )
    )
    await session.delete(record)
    audit.record(
        session,
        "integration.disconnected",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="integration",
        entity_id=record.id,
        ip=ip,
        details={"provider": provider.value},
    )
    await session.commit()


# ---- runtime -------------------------------------------------------------------------------------


async def capability_record(
    session: AsyncSession, company_id: uuid.UUID, provider: Provider, capability: Capability
) -> Integration | None:
    """The integration if ``capability`` is ENABLED and usable, else None."""
    record = await get_record(session, company_id, provider)
    if record is None or capability.value not in record.enabled_capabilities:
        return None
    view = build_view(SPECS[provider], record, user_connected=True)
    cap = next((c for c in view.capabilities if c.capability == capability), None)
    if cap is None or cap.state != CapabilityState.ENABLED:
        return None
    return record


async def require_capability(
    session: AsyncSession, company_id: uuid.UUID, provider: Provider, capability: Capability
) -> Integration:
    record = await capability_record(session, company_id, provider, capability)
    if record is None:
        spec = SPECS[provider]
        label = spec.capability(capability)
        raise IntegrationNotReadyError(
            f"{spec.name} {label.label if label else capability.value} is not enabled. "
            "An admin can set it up in Settings > Integrations.",
            details={"provider": SLUG_OF[provider], "capability": capability.value},
        )
    return record


async def flag_runtime_error(company_id: uuid.UUID, integration_id: uuid.UUID, code: str) -> None:
    """Record that a provider rejected our credentials at runtime (shown as ERROR in the UI).
    Uses its own transaction so a failed business operation cannot lose the flag."""
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id))
        record = await session.scalar(
            select(Integration).where(
                Integration.company_id == company_id, Integration.id == integration_id
            )
        )
        if record is not None:
            record.error_code = code[:64]
            record.error_at = datetime.now(UTC)
            await session.commit()
    logger.warning(
        "integration_runtime_error", extra={"integration_id": str(integration_id), "error": code}
    )


async def route_for(integration_id: uuid.UUID) -> IntegrationRoute | None:
    """Tenant routing for webhooks (system table, readable without tenant context)."""
    async with get_session_factory()() as session:
        route: IntegrationRoute | None = await session.get(IntegrationRoute, integration_id)
    return route


async def load_for_webhook(
    session: AsyncSession, integration_id: uuid.UUID, provider: Provider
) -> Integration | None:
    """Bind the tenant from the route, then load the integration under RLS."""
    route = await route_for(integration_id)
    if route is None or route.provider != provider.value:
        return None
    await set_tenant_context(session, TenantContext(company_id=route.company_id))
    record: Integration | None = await session.scalar(
        select(Integration).where(
            Integration.company_id == route.company_id, Integration.id == integration_id
        )
    )
    return record
