"""Health, request IDs, error schema, security headers, CORS, rate limiting, logging, config."""

import json
import logging
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.common.config import Settings, normalize_database_url
from app.common.logging import JsonFormatter, TextFormatter
from tests.conftest import DEFAULT_PASSWORD, Account

Register = Callable[..., Awaitable[Account]]


async def test_health_and_readiness(client: AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


async def test_request_id_generated_and_propagated(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert len(resp.headers["x-request-id"]) == 32
    echoed = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"
    # Invalid/hostile request IDs are replaced, not reflected.
    hostile = await client.get("/health", headers={"X-Request-ID": "<script>" + "a" * 100})
    assert hostile.headers["x-request-id"] != "<script>" + "a" * 100


async def test_unknown_route_uses_error_schema(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist", headers={"X-Request-ID": "req-404"})
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "not_found",
            "message": "Not Found",
            "details": None,
            "request_id": "req-404",
        }
    }


async def test_method_not_allowed(client: AsyncClient) -> None:
    resp = await client.delete("/api/v1/me")
    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"


async def test_security_headers(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


async def test_cors_allows_configured_origin_only(client: AsyncClient) -> None:
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-csrf-protection,content-type",
    }
    ok = await client.options(
        "/api/v1/auth/refresh", headers={"Origin": "https://app.example.com", **preflight}
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://app.example.com"
    assert ok.headers["access-control-allow-credentials"] == "true"

    bad = await client.options(
        "/api/v1/auth/refresh", headers={"Origin": "https://evil.example.net", **preflight}
    )
    assert "access-control-allow-origin" not in bad.headers


async def test_unhandled_exception_returns_generic_500(client: AsyncClient) -> None:
    from app.main import app

    @app.get("/__test_boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    try:
        resp = await client.get("/__test_boom", headers={"X-Request-ID": "boom-1"})
    finally:
        app.router.routes.pop()
    assert resp.status_code == 500
    assert resp.json()["error"] == {
        "code": "internal_error",
        "message": "An unexpected error occurred",
        "details": None,
        "request_id": "boom-1",
    }
    assert "secret internal detail" not in resp.text
    assert resp.headers["x-request-id"] == "boom-1"


async def test_login_rate_limited(
    client: AsyncClient,
    register: Register,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await register(client, "rl@example.com", "RL Co")
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    statuses = []
    for _ in range(5):
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "rl@example.com", "password": "wrong-password-1"}
        )
        statuses.append(resp.status_code)
    assert statuses == [401, 401, 401, 429, 429]
    assert resp.json()["error"]["code"] == "rate_limited"
    assert int(resp.headers["retry-after"]) >= 1


async def test_logs_are_json_with_context_and_no_secrets(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "log@example.com",
                "password": DEFAULT_PASSWORD,
                "full_name": "Log User",
                "company_name": "Log Co",
            },
            headers={"X-Request-ID": "log-req-1"},
        )
    assert resp.status_code == 201
    formatter = JsonFormatter()
    lines = [formatter.format(r) for r in caplog.records]
    access = [json.loads(line) for line in lines if '"msg": "request"' in line]
    assert access
    assert access[-1]["path"] == "/api/v1/auth/register"
    assert access[-1]["status"] == 201
    joined = "\n".join(lines)
    assert DEFAULT_PASSWORD not in joined
    assert resp.json()["access_token"] not in joined


def test_json_formatter_redacts_sensitive_extras() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    record.password = "hunter2"
    record.token = "abc"
    out = json.loads(JsonFormatter().format(record))
    assert out["password"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"


@pytest.mark.parametrize(
    ("raw", "url", "connect_args"),
    [
        (
            "postgres://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
            {},
        ),
        (
            "postgresql://u:p@host/db?sslmode=require",
            "postgresql+asyncpg://u:p@host/db",
            {"ssl": "require"},
        ),
        (
            "postgresql://u:p@host/db?sslmode=disable&application_name=x",
            "postgresql+asyncpg://u:p@host/db?application_name=x",
            {},
        ),
    ],
)
def test_database_url_normalization(raw: str, url: str, connect_args: dict[str, str]) -> None:
    assert normalize_database_url(raw) == (url, connect_args)


def test_non_postgres_url_rejected() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        normalize_database_url("mysql://u:p@h/db")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"jwt_secret": "short"}, "at least 32"),
        ({"app_env": "production", "cookie_secure": False}, "COOKIE_SECURE"),
        ({"app_env": "production", "jwt_secret": "change-me-" + "x" * 40}, "placeholder"),
        ({"app_env": "production", "cors_origins": "*"}, "Wildcard"),
        ({"cookie_samesite": "none", "cookie_secure": False}, "requires COOKIE_SECURE"),
    ],
)
def test_insecure_settings_rejected(overrides: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@h/db",
        "jwt_secret": "s" * 40,
    }
    with pytest.raises(ValueError, match=message):
        Settings(**{**base, **overrides})  # type: ignore[arg-type]


def test_text_formatter_includes_fields_and_redacts() -> None:
    record = logging.LogRecord("app.access", logging.INFO, __file__, 1, "request", None, None)
    record.path = "/api/v1/me"
    record.status = 200
    record.password = "hunter2"
    line = TextFormatter().format(record)
    assert "path=/api/v1/me" in line
    assert "status=200" in line
    assert "hunter2" not in line
    assert "password=[REDACTED]" in line
