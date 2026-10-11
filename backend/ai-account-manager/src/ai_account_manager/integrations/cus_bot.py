from __future__ import annotations

import httpx
from typing import Any


class CusBotDeliveryError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        delivery_unknown: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.delivery_unknown = delivery_unknown
        super().__init__(message)


async def send_topic_notification(
    *,
    bot_token: str,
    chat_id: int,
    thread_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> int:
    if not bot_token:
        raise CusBotDeliveryError(
            "CUS_BOT_NOT_CONFIGURED",
            "CUS Bot is not configured",
            retryable=True,
        )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            request_payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "text": text,
            }
            if reply_markup is not None:
                request_payload["reply_markup"] = reply_markup
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=request_payload,
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_UNAVAILABLE",
            "CUS Bot notification delivery is unavailable",
            retryable=True,
            delivery_unknown=True,
        ) from exc
    if (
        response.status_code != 200
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_REJECTED",
            "Telegram rejected CUS Bot notification",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_INVALID",
            "Telegram returned invalid notification evidence",
            retryable=True,
            delivery_unknown=True,
        )
    message_id = result.get("message_id")
    returned_thread = result.get("message_thread_id")
    returned_chat = result.get("chat")
    returned_chat_id = returned_chat.get("id") if isinstance(returned_chat, dict) else None
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
        or returned_thread != thread_id
        or returned_chat_id != chat_id
    ):
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_MISMATCH",
            "Telegram notification target evidence does not match the topic",
            retryable=False,
            delivery_unknown=True,
        )
    return message_id


async def replace_setup_button(
    *,
    bot_token: str,
    chat_id: int,
    message_id: int,
    deep_link: str,
    timeout_seconds: float = 10.0,
) -> None:
    """Refresh the existing setup message without creating another topic notice."""

    if not bot_token:
        raise CusBotDeliveryError(
            "CUS_BOT_NOT_CONFIGURED", "CUS Bot is not configured", retryable=True
        )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {
                        "inline_keyboard": [[{"text": "Настроить в ЦУС", "url": deep_link}]],
                    },
                },
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_UNAVAILABLE",
            "CUS Bot setup button update is unavailable",
            retryable=True,
            delivery_unknown=True,
        ) from exc
    if (
        response.status_code == 400
        and isinstance(payload, dict)
        and ("message is not modified" in str(payload.get("description", "")).lower())
    ):
        return
    if (
        response.status_code != 200
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_REJECTED",
            "Telegram rejected setup button update",
            retryable=response.status_code >= 500 or response.status_code == 429,
        )
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("message_id") != message_id:
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_MISMATCH",
            "Telegram setup button target evidence does not match",
            retryable=False,
            delivery_unknown=True,
        )
    returned_chat = result.get("chat")
    if not isinstance(returned_chat, dict) or returned_chat.get("id") != chat_id:
        raise CusBotDeliveryError(
            "CUS_BOT_DELIVERY_MISMATCH",
            "Telegram setup button chat evidence does not match",
            retryable=False,
            delivery_unknown=True,
        )
