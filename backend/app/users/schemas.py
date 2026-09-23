import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.common.schemas import APIModel

Phone = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 ()-]{6,32}$", max_length=32)
]


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = None
    phone: Phone | None = None
