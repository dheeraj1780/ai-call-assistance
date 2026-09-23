import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints

from app.common.schemas import APIModel

Name200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text5k = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class CompanySummary(APIModel):
    id: uuid.UUID
    name: str


class CompanyOut(APIModel):
    id: uuid.UUID
    name: str
    industry: str | None
    description: str | None
    website: str | None
    products_services: str | None
    target_customer: str | None
    ai_instructions: str | None
    transcript_retention_days: int
    created_at: datetime
    updated_at: datetime


class CompanyUpdate(BaseModel):
    """Partial update. There is deliberately no company id field: the tenant always comes
    from the authenticated principal."""

    model_config = ConfigDict(extra="forbid")

    name: Name200 | None = None
    industry: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] | None = None
    description: Text5k | None = None
    website: AnyHttpUrl | None = None
    products_services: Text5k | None = None
    target_customer: Text5k | None = None
    ai_instructions: Text5k | None = None
    transcript_retention_days: Annotated[int, Field(ge=1, le=365)] | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    joined_at: datetime
