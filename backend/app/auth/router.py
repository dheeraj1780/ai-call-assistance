from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.dependencies import require_csrf_protection
from app.auth.schemas import AuthResponse, LoginRequest, RegisterRequest
from app.common.config import Settings, get_settings
from app.common.db import get_db_session
from app.common.errors import ErrorResponse
from app.common.rate_limit import client_ip, enforce_auth_rate_limit
from app.tenants.schemas import CompanySummary
from app.users.schemas import UserOut

REFRESH_COOKIE = "cc_refresh"
AUTH_PATH = "/api/v1/auth"

router = APIRouter(prefix="/auth", tags=["auth"])

_errors: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=AUTH_PATH,
        domain=settings.cookie_domain,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=AUTH_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def _auth_response(
    result: service.AuthResult, response: Response, settings: Settings
) -> AuthResponse:
    _set_refresh_cookie(response, result.refresh_token, settings)
    return AuthResponse(
        access_token=result.access_token,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(result.user),
        company=CompanySummary.model_validate(result.company),
        role=result.role.value,
    )


@router.post(
    "/register", status_code=status.HTTP_201_CREATED, response_model=AuthResponse, responses=_errors
)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    enforce_auth_rate_limit(request, bucket="register")
    result = await service.register(
        session,
        settings,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        company_name=body.company_name,
        ip=client_ip(request),
    )
    return _auth_response(result, response, settings)


@router.post("/login", response_model=AuthResponse, responses=_errors)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    enforce_auth_rate_limit(request, bucket="login", extra_key=body.email)
    result = await service.login(
        session, settings, email=body.email, password=body.password, ip=client_ip(request)
    )
    return _auth_response(result, response, settings)


@router.post(
    "/refresh",
    response_model=AuthResponse,
    responses=_errors,
    dependencies=[Depends(require_csrf_protection)],
)
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    enforce_auth_rate_limit(request, bucket="refresh")
    result = await service.refresh(
        session, settings, raw_token=refresh_token, ip=client_ip(request)
    )
    return _auth_response(result, response, settings)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_errors,
    dependencies=[Depends(require_csrf_protection)],
)
async def logout(
    request: Request,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    await service.logout(session, raw_token=refresh_token, ip=client_ip(request))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(response, settings)
    return response
