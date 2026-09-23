"""Cross-tenant access attempts, at both the API and the database (RLS) layer."""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.auth.tokens import create_access_token
from app.common.config import Settings
from app.common.db import TenantContext
from app.tenants.models import Company, CompanyMember, MemberRole
from app.tenants.repository import CompanyRepository, MemberRepository
from tests.conftest import DEFAULT_PASSWORD, Account, add_member_directly, open_session

Register = Callable[..., Awaitable[Account]]


@pytest.fixture
async def two_tenants(client: AsyncClient, register: Register) -> tuple[Account, Account]:
    a = await register(client, "owner-a@example.com", "Company A")
    b = await register(client, "owner-b@example.com", "Company B")
    return a, b


# ---- API layer ---------------------------------------------------------------------------


async def test_each_tenant_sees_only_its_own_company(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, b = two_tenants
    ra = await client.get("/api/v1/companies/current", headers=a.headers)
    rb = await client.get("/api/v1/companies/current", headers=b.headers)
    assert ra.json()["id"] == str(a.company_id)
    assert ra.json()["name"] == "Company A"
    assert rb.json()["id"] == str(b.company_id)
    assert rb.json()["name"] == "Company B"


async def test_member_list_is_tenant_scoped(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, _b = two_tenants
    await add_member_directly(a.company_id, "rep-a@example.com", MemberRole.MEMBER)
    resp = await client.get("/api/v1/companies/current/members", headers=a.headers)
    assert resp.status_code == 200
    emails = {m["email"] for m in resp.json()["items"]}
    assert emails == {"owner-a@example.com", "rep-a@example.com"}
    assert resp.json()["total"] == 2


async def test_update_only_affects_own_tenant(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, b = two_tenants
    resp = await client.patch(
        "/api/v1/companies/current", headers=a.headers, json={"industry": "Manufacturing"}
    )
    assert resp.status_code == 200
    rb = await client.get("/api/v1/companies/current", headers=b.headers)
    assert rb.json()["industry"] is None


async def test_client_supplied_company_id_is_rejected(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, b = two_tenants
    resp = await client.patch(
        "/api/v1/companies/current",
        headers=a.headers,
        json={"id": str(b.company_id), "name": "Hijacked"},
    )
    assert resp.status_code == 422  # unknown fields are forbidden
    rb = await client.get("/api/v1/companies/current", headers=b.headers)
    assert rb.json()["name"] == "Company B"


async def test_forged_token_for_other_tenant_is_rejected(
    client: AsyncClient, two_tenants: tuple[Account, Account], settings: Settings
) -> None:
    """Even a correctly signed token cannot claim a company the user is not a member of."""
    a, b = two_tenants
    session = await open_session()
    async with session:
        from app.auth.models import AuthSession

        sid = await session.scalar(
            select(AuthSession.id).where(AuthSession.user_id == a.user_id).limit(1)
        )
    assert sid is not None
    forged = create_access_token(
        settings, user_id=a.user_id, company_id=b.company_id, session_id=sid
    )
    for path in ("/api/v1/companies/current", "/api/v1/me", "/api/v1/companies/current/members"):
        resp = await client.get(path, headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401, path


async def test_token_with_other_users_session_is_rejected(
    client: AsyncClient, two_tenants: tuple[Account, Account], settings: Settings
) -> None:
    a, b = two_tenants
    session = await open_session()
    async with session:
        from app.auth.models import AuthSession

        b_sid = await session.scalar(select(AuthSession.id).where(AuthSession.user_id == b.user_id))
    assert b_sid is not None
    forged = create_access_token(
        settings, user_id=a.user_id, company_id=a.company_id, session_id=b_sid
    )
    resp = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_member_role_cannot_update_company(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, _ = two_tenants
    await add_member_directly(a.company_id, "member@example.com", MemberRole.MEMBER)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "member@example.com", "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/companies/current", headers=headers)).status_code == 200
    resp = await client.patch("/api/v1/companies/current", headers=headers, json={"name": "X"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_admin_role_can_update_company(
    client: AsyncClient, two_tenants: tuple[Account, Account]
) -> None:
    a, _ = two_tenants
    await add_member_directly(a.company_id, "admin@example.com", MemberRole.ADMIN)
    login = await client.post(
        "/api/v1/auth/login", json={"email": "admin@example.com", "password": DEFAULT_PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await client.patch(
        "/api/v1/companies/current", headers=headers, json={"transcript_retention_days": 7}
    )
    assert resp.status_code == 200
    assert resp.json()["transcript_retention_days"] == 7


# ---- Repository / database layer (Row-Level Security) --------------------------------------


async def test_repository_cannot_read_other_tenant_even_by_id(
    two_tenants: tuple[Account, Account],
) -> None:
    """Simulates a buggy query that forgets the tenant filter: RLS still hides B from A."""
    a, b = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id, user_id=a.user_id))
    async with session:
        assert await CompanyRepository(session).get(b.company_id) is None
        assert await CompanyRepository(session).get(a.company_id) is not None
        unscoped = (await session.scalars(select(Company))).all()
        assert [c.id for c in unscoped] == [a.company_id]
        members, total = await MemberRepository(session).list_for_company(
            b.company_id, limit=50, offset=0
        )
        assert total == 0
        assert list(members) == []


async def test_session_without_tenant_context_sees_nothing(
    two_tenants: tuple[Account, Account],
) -> None:
    session = await open_session()
    async with session:
        assert await session.scalar(select(func.count()).select_from(Company)) == 0
        assert await session.scalar(select(func.count()).select_from(CompanyMember)) == 0


async def test_raw_sql_is_also_filtered(two_tenants: tuple[Account, Account]) -> None:
    a, _ = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        names = (await session.execute(text("SELECT name FROM companies"))).scalars().all()
    assert names == ["Company A"]


async def test_cannot_insert_member_into_other_tenant(
    two_tenants: tuple[Account, Account],
) -> None:
    a, b = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id, user_id=a.user_id))
    async with session:
        session.add(CompanyMember(company_id=b.company_id, user_id=uuid.uuid4(), role="MEMBER"))
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await session.flush()
        assert "row-level security" in str(exc.value)


async def test_cannot_update_other_tenant_rows(two_tenants: tuple[Account, Account]) -> None:
    a, b = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        result = await session.execute(
            text("UPDATE companies SET name = 'pwned' WHERE id = :id"), {"id": b.company_id}
        )
        assert result.rowcount == 0  # type: ignore[attr-defined]
        await session.commit()
    session = await open_session(TenantContext(company_id=b.company_id))
    async with session:
        assert await session.scalar(select(Company.name)) == "Company B"


async def test_cannot_move_own_row_into_other_tenant(two_tenants: tuple[Account, Account]) -> None:
    a, b = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        with pytest.raises((ProgrammingError, DBAPIError)):
            await session.execute(
                text("UPDATE company_members SET company_id = :b WHERE company_id = :a"),
                {"a": a.company_id, "b": b.company_id},
            )


async def test_audit_log_is_tenant_scoped_and_append_only(
    two_tenants: tuple[Account, Account],
) -> None:
    a, _b = two_tenants
    session = await open_session(TenantContext(company_id=a.company_id))
    async with session:
        visible = (await session.execute(text("SELECT company_id FROM audit_logs"))).scalars().all()
        assert visible
        assert set(visible) == {a.company_id}
        deleted = await session.execute(text("DELETE FROM audit_logs"))
        assert deleted.rowcount == 0  # type: ignore[attr-defined]
        updated = await session.execute(text("UPDATE audit_logs SET action = 'x'"))
        assert updated.rowcount == 0  # type: ignore[attr-defined]
