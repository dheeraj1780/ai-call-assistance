from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, StringConstraints

from app.tenants.schemas import CompanySummary
from app.users.schemas import UserOut


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _password_policy(value: str) -> str:
    if not value.strip():
        raise ValueError("Password must not be blank")
    return value


Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
# Length bounds: minimum per NIST SP 800-63B guidance; maximum caps hashing cost.
Password = Annotated[str, Field(min_length=10, max_length=128), AfterValidator(_password_policy)]
NonBlank200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RegisterRequest(BaseModel):
    email: Email
    password: Password
    full_name: NonBlank200
    company_name: NonBlank200


class LoginRequest(BaseModel):
    email: Email
    # No policy on login: only the stored hash decides. Upper bound still caps hashing cost.
    password: Annotated[str, Field(min_length=1, max_length=128)]


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type label
    expires_in: int
    user: UserOut
    company: CompanySummary
    role: str
