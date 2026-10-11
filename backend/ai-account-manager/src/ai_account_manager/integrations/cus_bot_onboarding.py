"""Idempotent setup notice shared by webhook and Runtime topic reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote

from ai_account_manager.core.config import AppSettings
from ai_account_manager.core.runtime_onboarding import (
    RuntimeOnboardingError,
    issue_topic_onboarding_token,
    mark_setup_button_refreshed,
    mark_setup_notice_delivered,
    renew_expired_topic_onboarding_record,
)
from ai_account_manager.integrations.cus_bot import replace_setup_button, send_topic_notification

_setup_delivery_lock = asyncio.Lock()


async def ensure_topic_setup_notice(
    settings: AppSettings,
    *,
    chat_id: int,
    thread_id: int,
    topic_name: str,
    token_factory: Callable[[], str] | None = None,
) -> int | None:
    """Send one notice for a CUS-unconfigured upstream topic, if needed.

    The lock closes the webhook/reconcile race within the single CUS service
    process. The durable setup_message_id suppresses later webhook replays and
    periodic scans. Neither caller creates Telegram Agent topic_config entries.
    """

    if chat_id not in settings.cus_allowed_forum_chat_ids:
        raise RuntimeOnboardingError("FORUM_NOT_ALLOWED", "Forum chat is not allowlisted")
    async with _setup_delivery_lock:
        try:
            issued = issue_topic_onboarding_token(
                settings.runtime_control_path,
                chat_id=chat_id,
                thread_id=thread_id,
                topic_name=topic_name,
                ttl_seconds=settings.cus_onboarding_token_ttl_seconds,
                now=datetime.now(UTC),
                token_factory=token_factory,
            )
        except RuntimeOnboardingError as exc:
            if exc.code == "TOPIC_ALREADY_BOUND":
                return None
            raise
        if issued.record.setup_message_id is not None:
            renewed = renew_expired_topic_onboarding_record(
                settings.runtime_control_path,
                onboarding_id=issued.record.onboarding_id,
                token_key=settings.cus_onboarding_token_key,
                ttl_seconds=settings.cus_onboarding_token_ttl_seconds,
            )
            if renewed is not None:
                await replace_setup_button(
                    bot_token=settings.cus_telegram_bot_token,
                    chat_id=renewed.record.chat_id,
                    message_id=renewed.record.setup_message_id,
                    deep_link=(
                        f"{settings.cus_miniapp_deep_link_base}{quote(renewed.token, safe='')}"
                    ),
                    timeout_seconds=settings.runtime_connector_timeout_seconds,
                )
                mark_setup_button_refreshed(
                    settings.runtime_control_path,
                    onboarding_id=renewed.record.onboarding_id,
                    token=renewed.token,
                    message_id=renewed.record.setup_message_id,
                )
            return None
        deep_link = f"{settings.cus_miniapp_deep_link_base}{quote(issued.token, safe='')}"
        message_id = await send_topic_notification(
            bot_token=settings.cus_telegram_bot_token,
            chat_id=issued.record.chat_id,
            thread_id=issued.record.thread_id,
            text="Проект ещё не настроен",
            reply_markup={
                "inline_keyboard": [[{"text": "Настроить в ЦУС", "url": deep_link}]],
            },
            timeout_seconds=settings.runtime_connector_timeout_seconds,
        )
        mark_setup_notice_delivered(
            settings.runtime_control_path,
            onboarding_id=issued.record.onboarding_id,
            token=issued.token,
            message_id=message_id,
        )
        return message_id
