from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from hmac import compare_digest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ai_account_manager.api.routes.auth import router as auth_router
from ai_account_manager.api.routes.codex import router as codex_router
from ai_account_manager.api.routes.cus_bot import router as cus_bot_router
from ai_account_manager.api.routes.health import router as health_router
from ai_account_manager.api.routes.launcher import router as launcher_router
from ai_account_manager.api.routes.onboarding import router as onboarding_router
from ai_account_manager.api.routes.providers import router as providers_router
from ai_account_manager.api.routes.runtime import router as runtime_router
from ai_account_manager.api.routes.sessions import router as sessions_router
from ai_account_manager.api.routes.upgrade import router as upgrade_router
from ai_account_manager.core.config import get_settings
from ai_account_manager.integrations.runtime_reconcile_service import runtime_reconcile_loop
from ai_account_manager.auth.telegram import (
    TelegramInitDataError,
    validate_telegram_init_data,
)


def _is_preview_protected_path(path: str) -> bool:
    return (
        path.startswith("/api/")
        or path.startswith("/docs/")
        or path.startswith("/redoc/")
        or path
        in {
            "/api",
            "/docs",
            "/docs/oauth2-redirect",
            "/openapi.json",
            "/redoc",
        }
    )


def _valid_preview_token(request: Request, expected: str) -> bool:
    if not expected:
        return True
    supplied = (
        request.headers.get("X-CUS-Preview-Token")
        or request.query_params.get("preview_token")
        or request.cookies.get("cus_preview_token")
        or ""
    )
    return bool(supplied) and compare_digest(supplied, expected)


def _register_miniapp(app: FastAPI, root: Path) -> None:
    index_path = root / "index.html"
    assets_path = root / "assets"
    if not index_path.is_file() or not assets_path.is_dir():
        return

    app.mount(
        "/miniapp/assets",
        StaticFiles(directory=assets_path),
        name="miniapp-assets",
    )

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/miniapp/", status_code=307)

    @app.get("/miniapp", include_in_schema=False)
    @app.get("/miniapp/", include_in_schema=False)
    async def miniapp_index() -> FileResponse:
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        reconcile_task: asyncio.Task[None] | None = None
        if settings.runtime_reconcile_enabled:
            reconcile_task = asyncio.create_task(
                runtime_reconcile_loop(settings),
                name="cus-runtime-reconcile",
            )
        try:
            yield
        finally:
            if reconcile_task is not None:
                reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reconcile_task

    app = FastAPI(
        title="AI Account Manager",
        version="0.11.0",
        description="Provider-agnostic AI account/profile/quota backend.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def access_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _is_preview_protected_path(request.url.path):
            return await call_next(request)

        onboarding_path = request.url.path.startswith("/api/onboarding/")
        if (
            not onboarding_path
            and settings.preview_access_token
            and _valid_preview_token(request, settings.preview_access_token)
        ):
            return await call_next(request)

        # Topic onboarding belongs exclusively to the separate CUS Bot.  Falling
        # back to the working Telegram Agent token here would couple credentials
        # across the two trust boundaries and accept initData issued for the wrong
        # bot identity.
        telegram_auth_token = (
            settings.cus_telegram_bot_token
            if onboarding_path
            else settings.cus_telegram_bot_token or settings.telegram_bot_token
        )
        telegram_auth_enabled = bool(telegram_auth_token)
        if (
            settings.app_env.lower() == "production"
            and not settings.preview_access_token
            and not telegram_auth_enabled
        ):
            return JSONResponse(
                status_code=503,
                content={"detail": "Production authentication is not configured"},
            )

        if telegram_auth_enabled:
            try:
                principal = validate_telegram_init_data(
                    request.headers.get("X-Telegram-Init-Data", ""),
                    bot_token=telegram_auth_token,
                    max_age_seconds=settings.telegram_init_data_max_age_seconds,
                )
            except TelegramInitDataError as exc:
                return JSONResponse(status_code=401, content={"detail": str(exc)})

            if (
                not settings.telegram_allowed_user_ids
                or principal.user_id not in settings.telegram_allowed_user_ids
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Telegram user is not allowed."},
                )
            request.state.telegram_user = principal
            return await call_next(request)

        if settings.preview_access_token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Preview access token or Telegram authorization is required."},
            )

        return await call_next(request)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(providers_router, prefix="/api")
    app.include_router(runtime_router, prefix="/api")
    app.include_router(onboarding_router, prefix="/api")
    app.include_router(codex_router)
    app.include_router(launcher_router)
    app.include_router(sessions_router)
    app.include_router(upgrade_router)
    app.include_router(cus_bot_router)
    _register_miniapp(app, settings.miniapp_static_root)
    return app
