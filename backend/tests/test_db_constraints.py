"""Database-level constraints, role safety and extensions."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.common.db import TenantContext, check_db_role_safety, get_engine
from app.tenants.models import Company, CompanyMember
from app.users.models import User
from tests.conftest import open_session


def _user(email: str, name: str = "Name") -> User:
    return User(id=uuid.uuid4(), email=email, password_hash="x", full_name=name)


async def _expect_integrity_error(obj: object, ctx: TenantContext | None = None) -> str:
    session = await open_session(ctx)
    async with session:
        session.add(obj)
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        return str(exc.value.orig)


async def test_email_must_be_lowercase() -> None:
    msg = await _expect_integrity_error(_user("Mixed@Example.com"))
    assert "ck_users_email_lowercase" in msg


async def test_email_unique() -> None:
    session = await open_session()
    async with session:
        session.add(_user("same@example.com"))
        await session.commit()
    msg = await _expect_integrity_error(_user("same@example.com"))
    assert "uq_users_email_lower" in msg


async def test_blank_full_name_rejected() -> None:
    msg = await _expect_integrity_error(_user("blank@example.com", name="   "))
    assert "ck_users_full_name_not_blank" in msg


@pytest.mark.parametrize("days", [0, 366, -1])
async def test_retention_days_range(days: int) -> None:
    cid = uuid.uuid4()
    msg = await _expect_integrity_error(
        Company(id=cid, name="Co", transcript_retention_days=days),
        ctx=TenantContext(company_id=cid),
    )
    assert "ck_companies_transcript_retention_days_range" in msg


async def test_blank_company_name_rejected() -> None:
    cid = uuid.uuid4()
    msg = await _expect_integrity_error(
        Company(id=cid, name="  "), ctx=TenantContext(company_id=cid)
    )
    assert "ck_companies_name_not_blank" in msg


async def test_default_retention_is_30_days() -> None:
    cid = uuid.uuid4()
    session = await open_session(TenantContext(company_id=cid))
    async with session:
        await session.execute(
            text("INSERT INTO companies (id, name) VALUES (:id, 'Co')"), {"id": cid}
        )
        days = await session.scalar(
            text("SELECT transcript_retention_days FROM companies WHERE id = :id"), {"id": cid}
        )
    assert days == 30


async def _company_and_user(session_ctx_company: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    uid = uuid.uuid4()
    session = await open_session(TenantContext(company_id=session_ctx_company))
    async with session:
        session.add(Company(id=session_ctx_company, name="Co"))
        session.add(User(id=uid, email=f"{uid.hex}@example.com", password_hash="x", full_name="N"))
        await session.commit()
    return session_ctx_company, uid


async def test_invalid_role_rejected() -> None:
    cid, uid = await _company_and_user(uuid.uuid4())
    msg = await _expect_integrity_error(
        CompanyMember(company_id=cid, user_id=uid, role="SUPERADMIN"),
        ctx=TenantContext(company_id=cid),
    )
    assert "ck_company_members_role_valid" in msg


async def test_user_can_belong_to_only_one_company() -> None:
    c1, uid = await _company_and_user(uuid.uuid4())
    session = await open_session(TenantContext(company_id=c1))
    async with session:
        session.add(CompanyMember(company_id=c1, user_id=uid, role="OWNER"))
        await session.commit()
    c2 = uuid.uuid4()
    session = await open_session(TenantContext(company_id=c2))
    async with session:
        session.add(Company(id=c2, name="Second"))
        await session.commit()
    msg = await _expect_integrity_error(
        CompanyMember(company_id=c2, user_id=uid, role="OWNER"), ctx=TenantContext(company_id=c2)
    )
    assert "uq_company_members_user_id" in msg


async def test_member_foreign_keys_enforced() -> None:
    cid = uuid.uuid4()
    session = await open_session(TenantContext(company_id=cid))
    async with session:
        session.add(Company(id=cid, name="Co"))
        await session.commit()
    msg = await _expect_integrity_error(
        CompanyMember(company_id=cid, user_id=uuid.uuid4(), role="MEMBER"),
        ctx=TenantContext(company_id=cid),
    )
    assert "fk_company_members_user_id_users" in msg


async def test_app_db_role_does_not_bypass_rls() -> None:
    engine = get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False
    await check_db_role_safety(engine, strict=True)  # must not raise


async def test_pgvector_extension_available() -> None:
    session = await open_session()
    async with session:
        version = await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        distance = await session.scalar(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"))
    assert version is not None
    assert distance == pytest.approx(1.0)


async def test_rls_forced_on_tenant_tables() -> None:
    session = await open_session()
    async with session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('companies', 'company_members', 'audit_logs')"
                )
            )
        ).all()
    assert len(rows) == 3
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows)
