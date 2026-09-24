"""Production configuration guards (pure; no DB)."""

from typing import Any

import pytest

from app.common.config import Settings

SECRET = "s" * 40
BASE: dict[str, Any] = {
    "database_url": "postgresql://u:p@h/db",
    "jwt_secret": SECRET,
    "app_env": "production",
    "cookie_secure": True,
    "cors_origins": "https://app.example.com",
}


def make(**overrides: Any) -> Settings:
    return Settings(**{**BASE, **overrides})


def test_mock_providers_are_refused_in_production() -> None:
    with pytest.raises(ValueError, match="Mock providers are configured in production"):
        make(telephony_webhook_secret=SECRET, token_encryption_key=SECRET)


def test_staging_can_opt_in_to_mocks_but_still_needs_secrets() -> None:
    with pytest.raises(ValueError, match="TELEPHONY_WEBHOOK_SECRET"):
        make(allow_mock_providers_in_production=True, token_encryption_key=SECRET)
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        make(allow_mock_providers_in_production=True, telephony_webhook_secret=SECRET)
    with pytest.raises(ValueError, match="at least 32"):
        make(
            allow_mock_providers_in_production=True,
            telephony_webhook_secret="short",
            token_encryption_key=SECRET,
        )
    ok = make(
        allow_mock_providers_in_production=True,
        telephony_webhook_secret=SECRET,
        token_encryption_key=SECRET,
    )
    assert ok.is_production


def test_real_provider_requires_its_key() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        Settings(database_url="postgresql://u:p@h/db", jwt_secret=SECRET, ai_provider="anthropic")
    with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
        Settings(
            database_url="postgresql://u:p@h/db", jwt_secret=SECRET, calendar_provider="google"
        )
