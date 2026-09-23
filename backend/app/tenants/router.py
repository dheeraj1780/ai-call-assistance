from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import service as audit
from app.auth.dependencies import Principal, get_principal, require_roles
from app.common.db import get_db_session
from app.common.errors import NotFoundError
from app.common.rate_limit import client_ip
from app.common.schemas import Page, PageParams
from app.tenants.models import Company, MemberRole
from app.tenants.repository import CompanyRepository, MemberRepository
from app.tenants.schemas import CompanyOut, CompanyUpdate, MemberOut

router = APIRouter(prefix="/companies", tags=["companies"])


async def _current_company(session: AsyncSession, principal: Principal) -> Company:
    company = await CompanyRepository(session).get(principal.company_id)
    if company is None:
        raise NotFoundError()
    return company


@router.get("/current", response_model=CompanyOut)
async def get_current_company(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Company:
    return await _current_company(session, principal)


@router.patch("/current", response_model=CompanyOut)
async def update_current_company(
    body: CompanyUpdate,
    request: Request,
    principal: Principal = Depends(require_roles(MemberRole.OWNER, MemberRole.ADMIN)),
    session: AsyncSession = Depends(get_db_session),
) -> Company:
    company = await _current_company(session, principal)
    changes = body.model_dump(exclude_unset=True, mode="json")
    for field, value in changes.items():
        if field in {"name", "transcript_retention_days"} and value is None:
            continue  # non-nullable fields: ignore explicit nulls
        setattr(company, field, value)
    audit.record(
        session,
        "company.updated",
        company_id=principal.company_id,
        actor_user_id=principal.user_id,
        entity_type="company",
        entity_id=company.id,
        ip=client_ip(request),
        details={"fields": sorted(changes)},  # field names only, never values
    )
    await session.commit()
    await session.refresh(company)
    return company


@router.get("/current/members", response_model=Page[MemberOut])
async def list_members(
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> Page[MemberOut]:
    rows, total = await MemberRepository(session).list_for_company(
        principal.company_id, limit=page.limit, offset=page.offset
    )
    return Page(
        items=[
            MemberOut(
                user_id=user.id,
                email=user.email,
                full_name=user.full_name,
                role=member.role,
                joined_at=member.created_at,
            )
            for member, user in rows
        ],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
