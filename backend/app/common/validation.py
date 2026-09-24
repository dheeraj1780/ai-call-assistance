"""Reusable Pydantic field types."""

import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, EmailStr, StringConstraints

_PHONE_ALLOWED = re.compile(r"^\+?[0-9]{6,15}$")


def _normalize_phone(value: str) -> str:
    """Strip common separators. Full E.164 normalisation (country inference) is deferred to the
    telephony phase, where provider requirements are known."""
    compact = re.sub(r"[\s().-]", "", value)
    if not _PHONE_ALLOWED.match(compact):
        raise ValueError("Phone must contain 6-15 digits, optionally starting with +")
    return compact


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _lower(value: str) -> str:
    return value.strip().lower()


_Blank = BeforeValidator(_blank_to_none)

Phone = Annotated[str, _Blank, AfterValidator(_normalize_phone)]
Email = Annotated[EmailStr, _Blank, AfterValidator(_lower)]

# Required, trimmed, non-empty strings.
Name200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Title300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Body10k = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)]

# Optional, trimmed strings; blank input becomes None.
Text120 = Annotated[str, _Blank, StringConstraints(strip_whitespace=True, max_length=120)]
Text200 = Annotated[str, _Blank, StringConstraints(strip_whitespace=True, max_length=200)]
Text2k = Annotated[str, _Blank, StringConstraints(strip_whitespace=True, max_length=2_000)]
Text5k = Annotated[str, _Blank, StringConstraints(strip_whitespace=True, max_length=5_000)]
Text10k = Annotated[str, _Blank, StringConstraints(strip_whitespace=True, max_length=10_000)]
