"""Application configuration.

All environment-specific configuration comes from environment variables (or a local
`.env` file in development). Nothing cloud-specific is hard-coded.
"""

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "test", "production"]

_SSL_QUERY_PARAMS = {"sslmode", "ssl"}


def normalize_database_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    """Convert a standard PostgreSQL URL into an asyncpg SQLAlchemy URL.

    Hosting providers (e.g. Render) hand out ``postgres://`` / ``postgresql://`` URLs,
    possibly with ``?sslmode=require``. asyncpg does not understand ``sslmode``, so it is
    translated into a connect argument instead.
    """
    parts = urlsplit(raw_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL")

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    connect_args: dict[str, Any] = {}
    ssl_value = None
    for key in _SSL_QUERY_PARAMS:
        if key in query:
            ssl_value = query.pop(key)
    if ssl_value and ssl_value not in {"disable", "false", "0"}:
        # asyncpg accepts libpq-style mode names: allow/prefer/require/verify-ca/verify-full
        connect_args["ssl"] = ssl_value

    url = urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, urlencode(query), ""))
    return url, connect_args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnv = "development"
    app_name: str = "MSME Call Copilot"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100)

    jwt_secret: SecretStr
    jwt_issuer: str = "callcopilot"
    jwt_audience: str = "callcopilot-api"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)
    session_max_age_days: int = Field(default=30, ge=1, le=180)
    # Reusing a rotated refresh token within this window is treated as a benign concurrent
    # refresh (multiple tabs) instead of theft. 0 disables the grace window.
    refresh_reuse_grace_seconds: int = Field(default=10, ge=0, le=60)

    cookie_secure: bool = True
    cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    cookie_domain: str | None = None

    # Comma-separated list of allowed browser origins, e.g. "https://app.example.com".
    cors_origins: str = ""

    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)
    # Number of trusted reverse proxies in front of the API (see rate_limit.resolve_client_ip).
    # 0 = ignore X-Forwarded-For entirely. Must be measured for the actual deployment.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
    # Enables GET /health/client-ip (echoes the caller's own proxy headers) for measuring the
    # proxy chain during deployment verification. Keep disabled otherwise.
    diagnostics_enabled: bool = False

    # Refuse to start (production) / warn (other envs) if the DB role bypasses RLS.
    enforce_db_role_safety: bool = True

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_url(self) -> tuple[str, dict[str, Any]]:
        return normalize_database_url(self.database_url)

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("COOKIE_SAMESITE=none requires COOKIE_SECURE=true")
        if self.is_production:
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
            if "change-me" in secret.lower():
                raise ValueError("JWT_SECRET still has the placeholder value")
            if "*" in self.cors_origin_list:
                raise ValueError("Wildcard CORS origins are not allowed in production")
        normalize_database_url(self.database_url)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
