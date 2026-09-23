from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import Principal, get_principal
from app.common.db import get_db_session
from app.common.errors import NotFoundError
from app.tenants.repository import CompanyRepository
from app.tenants.schemas import CompanySummary
from app.users.models import User
from app.users.schemas import UserOut, UserUpdate

router = APIRouter(tags=["users"])


class MeResponse(BaseModel):
    user: UserOut
    company: CompanySummary
    role: str


async def _me(session: AsyncSession, principal: Principal) -> MeResponse:
    user = await session.get(User, principal.user_id)
    company = await CompanyRepository(session).get(principal.company_id)
    if user is None or company is None:
        raise NotFoundError()
    return MeResponse(
        user=UserOut.model_validate(user),
        company=CompanySummary.model_validate(company),
        role=principal.role.value,
    )


@router.get("/me", response_model=MeResponse)
async def get_me(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    return await _me(session, principal)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    body: UserUpdate,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    user = await session.get(User, principal.user_id)
    if user is None:
        raise NotFoundError()
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "full_name" and value is None:
            continue
        setattr(user, field, value)
    await session.commit()
    return await _me(session, principal)
