from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ai_account_manager.auth.telegram import TelegramInitData

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/telegram")
async def telegram_session(request: Request) -> dict[str, object]:
    principal = getattr(request.state, "telegram_user", None)
    if not isinstance(principal, TelegramInitData):
        raise HTTPException(status_code=401, detail="Telegram authorization is required.")
    return {
        "status": "ok",
        "user": {
            "id": principal.user_id,
            "username": principal.username,
            "first_name": principal.first_name,
            "last_name": principal.last_name,
        },
        "auth_date": principal.auth_date,
    }
