from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ai_account_manager.auth.cus_bot_webhook import (
    deterministic_onboarding_token,
    verify_webhook_secret,
)
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.runtime_onboarding import RuntimeOnboardingError
from ai_account_manager.integrations.cus_bot import CusBotDeliveryError
from ai_account_manager.integrations.cus_bot_onboarding import ensure_topic_setup_notice

router = APIRouter(tags=["cus-bot"])
# The deployment does not configure the root logger; inherit Uvicorn's handler
# so sanitized webhook outcome records actually reach the service journal.
logger = logging.getLogger("uvicorn.error.cus_bot")


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


@router.post("/telegram/cus-bot/webhook", include_in_schema=False)
async def cus_bot_webhook(request: Request) -> dict[str, object]:
    settings = get_settings()
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not verify_webhook_secret(supplied_secret, settings.cus_bot_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid CUS Bot webhook secret")
    if not settings.cus_miniapp_deep_link_base or not settings.cus_onboarding_token_key:
        raise HTTPException(status_code=503, detail="CUS Bot onboarding is not configured")
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Invalid Telegram update")
    update_kind = next(
        (key for key in ("message", "edited_message", "channel_post") if key in update),
        "other",
    )
    raw_message = update.get(update_kind)
    logger.info(
        "CUS Bot webhook update kind=%s topic_created=%s",
        update_kind,
        isinstance(raw_message, dict) and isinstance(raw_message.get("forum_topic_created"), dict),
    )
    message = update.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("forum_topic_created"), dict):
        return {}
    chat = message.get("chat")
    created = message["forum_topic_created"]
    if not isinstance(chat, dict) or not isinstance(created, dict):
        return {}
    try:
        update_id = _required_int(update, "update_id")
        chat_id = _required_int(chat, "id")
        thread_id = _required_int(message, "message_thread_id")
        _required_int(message, "date")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if chat_id not in settings.cus_allowed_forum_chat_ids:
        return {}
    logger.info(
        "CUS Bot topic-created update chat_id=%d thread_id=%d update_id=%d is_forum=%r",
        chat_id,
        thread_id,
        update_id,
        chat.get("is_forum"),
    )
    # Telegram marks Chat.is_forum optional. The allowlisted supergroup and the
    # forum_topic_created service message establish the forum identity even if
    # that optional flag is omitted from this particular update.
    if chat.get("type") != "supergroup" or chat.get("is_forum") is False or thread_id <= 0:
        return {}
    topic_name = created.get("name")
    if not isinstance(topic_name, str) or not topic_name.strip():
        raise HTTPException(status_code=400, detail="Telegram topic name is missing")
    token = deterministic_onboarding_token(
        token_key=settings.cus_onboarding_token_key,
        update_id=update_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    try:
        message_id = await ensure_topic_setup_notice(
            settings,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_name=topic_name,
            token_factory=lambda: token,
        )
    except CusBotDeliveryError as exc:
        logger.warning(
            "CUS Bot setup notice failed chat_id=%d thread_id=%d code=%s delivery_unknown=%s",
            chat_id,
            thread_id,
            exc.code,
            exc.delivery_unknown,
        )
        raise HTTPException(
            status_code=503 if exc.retryable else 502,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except RuntimeOnboardingError as exc:
        logger.error(
            "CUS Bot setup notice failed chat_id=%d thread_id=%d code=%s",
            chat_id,
            thread_id,
            exc.code,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if message_id is not None:
        logger.info(
            "CUS Bot setup notice confirmed chat_id=%d thread_id=%d message_id=%d",
            chat_id,
            thread_id,
            message_id,
        )
    return {}
