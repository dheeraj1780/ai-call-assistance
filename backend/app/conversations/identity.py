"""Identifier normalisation for safe contact matching (pure functions, unit-tested)."""

import re

from app.common.config import get_settings

_DIGITS = re.compile(r"\D")


def normalize_phone(value: str | None, default_country_code: str | None = None) -> str | None:
    """Return an E.164 number WITHOUT '+' (the format WhatsApp uses for wa_id), or None.

    - "+91 98765 43210", "0091-9876543210" -> "919876543210"
    - "09876543210" / "9876543210" (national) -> default country code + national number
    Numbers that cannot be interpreted unambiguously return None (never guessed).
    """
    if not value:
        return None
    raw = value.strip()
    digits = _DIGITS.sub("", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        result = digits
    elif digits.startswith("00"):
        result = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        result = _cc(default_country_code) + digits[1:]
    elif len(digits) == 10:
        result = _cc(default_country_code) + digits
    else:
        result = digits
    return result if 8 <= len(result) <= 15 else None


def _cc(explicit: str | None) -> str:
    return explicit or get_settings().default_phone_country_code


def phone_variants(normalized: str, default_country_code: str | None = None) -> list[str]:
    """Digit strings a contact's stored phone may reduce to for the same number."""
    cc = _cc(default_country_code)
    variants = {normalized}
    if normalized.startswith(cc):
        national = normalized[len(cc) :]
        variants.update({national, "0" + national, "00" + normalized})
    return sorted(variants)


def normalize_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    return value.strip().lower()
