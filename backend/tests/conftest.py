"""Test configuration.

Tests run against a REAL PostgreSQL database given by TEST_DATABASE_URL (environment or
backend/.env). The database is migrated to head once per session and every table is
truncated after each test, so it must be a dedicated database whose name ends in "_test".
"""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _read_dotenv_value(key: str) -> str | None:
    env_file = BACKEND_DIR / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name == key:
            return value.strip()
    return None


_test_db_url = os.environ.get("TEST_DATABASE_URL") or _read_dotenv_value("TEST_DATABASE_URL")
if not _test_db_url:
    raise RuntimeError("TEST_DATABASE_URL is not set (environment or backend/.env)")
if not _test_db_url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
    raise RuntimeError("Refusing to run: TEST_DATABASE_URL database name must end with '_test'")


def _db_identity(url: str) -> tuple[str, str]:
    """(host:port, database) of a URL, ignoring driver and credentials."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.strip())
    host = (parts.hostname or "").replace("127.0.0.1", "localhost")
    return f"{host}:{parts.port or 5432}", parts.path.lstrip("/").split("?", 1)[0]


# Tests TRUNCATE every table: never let them touch the application database.
_app_db_url = os.environ.get("DATABASE_URL") or _read_dotenv_value("DATABASE_URL")
if _app_db_url and _db_identity(_app_db_url) == _db_identity(_test_db_url):
    raise RuntimeError("Refusing to run: TEST_DATABASE_URL points at the application database")

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": _test_db_url,
        "JWT_SECRET": "test-secret-" + "x" * 40,
        "COOKIE_SECURE": "true",
        "COOKIE_SAMESITE": "strict",
        "CORS_ORIGINS": "https://app.example.com",
        "RATE_LIMIT_ENABLED": "true",
        "RATE_LIMIT_AUTH_PER_MINUTE": "1000",
        "LOG_JSON": "true",
        "SIMULATION_UTTERANCE_DELAY_SECONDS": "0",
        # Tests never use real providers, whatever backend/.env configures for local development.
        "AI_PROVIDER": "mock",
        "STT_PROVIDER": "mock",
        "TELEPHONY_PROVIDER": "mock",
        "CALENDAR_PROVIDER": "mock",
        "EMBEDDING_PROVIDER": "hashing",
        # ... and never a real LLM for message assistance, even when .env enables Gemini.
        "AI_MESSAGE_ASSIST_PROVIDER": "inherit",
        "PUBLIC_BASE_URL": "http://localhost:8000",
        "TOKEN_ENCRYPTION_KEY": "test-token-key-" + "y" * 40,
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.ai.gateway import set_ai_provider  # noqa: E402
from app.ai.mock_provider import MockAIProvider  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.common import handlers as _handlers  # noqa: E402, F401
from app.common.config import get_settings  # noqa: E402
from app.common.db import TenantContext, get_session_factory, set_tenant_context  # noqa: E402
from app.common.rate_limit import limiter  # noqa: E402
from app.integrations.providers import factory as integration_factory  # noqa: E402
from app.live import session as live_sessions  # noqa: E402
from app.live.hub import hub  # noqa: E402
from app.main import app  # noqa: E402
from app.speech.provider import MockSpeechToTextProvider, set_stt_provider  # noqa: E402
from app.telephony.provider import MockTelephonyProvider, set_telephony_provider  # noqa: E402
from app.tenants.models import CompanyMember, MemberRole  # noqa: E402
from app.users.models import User  # noqa: E402

DEFAULT_PASSWORD = "correct-horse-battery"
CSRF = {"X-CSRF-Protection": "1"}
TABLES = (
    "message_drafts, communication_events, communication_participants, communication_messages, "
    "communication_sessions, contact_identities, integration_subscriptions, "
    "integration_user_connections, integrations, integration_routes, "
    "integration_webhook_receipts, "
    "follow_up_drafts, call_summaries, "
    "knowledge_chunks, knowledge_documents, call_notes, copilot_insights, transcript_segments, "
    "telephony_webhook_events, call_routes, "
    "calendar_events, calendar_connections, jobs, ai_usage_records, agenda_items, "
    "timeline_events, action_items, calls, contact_notes, contacts, "
    "audit_logs, refresh_tokens, auth_sessions, company_members, companies, users"
)


def alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> None:
    command.upgrade(alembic_config(), "head")


@pytest.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    limiter.reset()
    set_ai_provider(MockAIProvider())
    set_telephony_provider(MockTelephonyProvider())
    set_stt_provider(MockSpeechToTextProvider())
    integration_factory.set_http_transport(None)
    integration_factory.reset_mocks()
    hub.reset()
    yield
    from app.telephony.service import wait_all_finalized

    await wait_all_finalized()
    await live_sessions.close_all()
    async with get_session_factory()() as session:
        await session.execute(text(f"TRUNCATE {TABLES} CASCADE"))
        await session.commit()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as c:
        yield c


@pytest.fixture
def make_client() -> Callable[[], AsyncClient]:
    """Independent clients (separate cookie jars) for multi-user scenarios."""

    def _make() -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")

    return _make


@dataclass
class Account:
    email: str
    password: str
    access_token: str
    user_id: uuid.UUID
    company_id: uuid.UUID
    role: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def register_account(
    client: AsyncClient,
    email: str,
    company_name: str,
    password: str = DEFAULT_PASSWORD,
    full_name: str = "Test User",
) -> Account:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "full_name": full_name,
            "company_name": company_name,
        },
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return Account(
        email=email,
        password=password,
        access_token=body["access_token"],
        user_id=uuid.UUID(body["user"]["id"]),
        company_id=uuid.UUID(body["company"]["id"]),
        role=body["role"],
    )


@pytest.fixture
def register() -> Callable[..., Awaitable[Account]]:
    return register_account


async def add_member_directly(
    company_id: uuid.UUID, email: str, role: MemberRole, password: str = DEFAULT_PASSWORD
) -> uuid.UUID:
    """Create a user inside an existing company (there is no invite API in Phase 1)."""
    user_id = uuid.uuid4()
    async with get_session_factory()() as session:
        await set_tenant_context(session, TenantContext(company_id=company_id, user_id=user_id))
        session.add(
            User(id=user_id, email=email, password_hash=hash_password(password), full_name="Member")
        )
        await session.flush()
        session.add(CompanyMember(company_id=company_id, user_id=user_id, role=role))
        await session.commit()
    return user_id


async def open_session(ctx: TenantContext | None = None) -> AsyncSession:
    session = get_session_factory()()
    if ctx is not None:
        await set_tenant_context(session, ctx)
    return session


@pytest.fixture
def settings() -> Any:
    return get_settings()
