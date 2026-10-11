from __future__ import annotations

import httpx


class TelegramTopicAccessError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


async def verify_forum_member_access(
    *,
    bot_token: str,
    chat_id: int,
    user_id: int,
    timeout_seconds: float = 5.0,
) -> None:
    """Verify Mini App principal membership using the separate CUS Bot."""

    if not bot_token:
        raise TelegramTopicAccessError(
            "CUS_BOT_NOT_CONFIGURED",
            "CUS Bot is not configured",
            retryable=True,
        )
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/getChatMember",
                json={"chat_id": chat_id, "user_id": user_id},
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramTopicAccessError(
            "TELEGRAM_ACCESS_UNAVAILABLE",
            "Telegram membership verification is unavailable",
            retryable=True,
        ) from exc
    if (
        response.status_code != 200
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        raise TelegramTopicAccessError(
            "TELEGRAM_ACCESS_UNAVAILABLE",
            "Telegram membership verification is unavailable",
            retryable=True,
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TelegramTopicAccessError(
            "TELEGRAM_ACCESS_UNAVAILABLE",
            "Telegram membership verification returned invalid data",
            retryable=True,
        )
    status = result.get("status")
    has_access = status in {"creator", "administrator", "member"} or (
        status == "restricted" and result.get("is_member") is True
    )
    if not has_access:
        raise TelegramTopicAccessError(
            "TELEGRAM_TOPIC_FORBIDDEN",
            "User is not a member of the Telegram forum",
            retryable=False,
        )
