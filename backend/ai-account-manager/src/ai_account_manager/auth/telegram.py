from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class TelegramInitDataError(ValueError):
    """Raised when Telegram Mini App initData is missing or invalid."""


@dataclass(frozen=True)
class TelegramInitData:
    user_id: int
    username: str | None
    first_name: str
    last_name: str | None
    auth_date: int
    query_id: str | None


def _data_check_string(items: list[tuple[str, str]], *, drop_signature: bool) -> str:
    excluded = {"hash"}
    if drop_signature:
        excluded.add("signature")
    return "\n".join(
        f"{key}={value}"
        for key, value in sorted(items)
        if key not in excluded
    )


def _expected_hash(data_check_string: str, bot_token: str) -> str:
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_telegram_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    now: int | None = None,
) -> TelegramInitData:
    if not raw_init_data:
        raise TelegramInitDataError("Telegram initData is required.")
    if not bot_token:
        raise TelegramInitDataError("Telegram bot token is not configured.")

    try:
        items = parse_qsl(raw_init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramInitDataError("Telegram initData is malformed.") from exc

    fields = dict(items)
    supplied_hash = fields.get("hash", "")
    if not supplied_hash:
        raise TelegramInitDataError("Telegram initData hash is missing.")

    candidates = [_data_check_string(items, drop_signature=False)]
    if "signature" in fields:
        candidates.append(_data_check_string(items, drop_signature=True))

    if not any(
        hmac.compare_digest(_expected_hash(candidate, bot_token), supplied_hash)
        for candidate in candidates
    ):
        raise TelegramInitDataError("Telegram initData signature is invalid.")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TelegramInitDataError("Telegram initData auth_date is invalid.") from exc

    current_time = int(time.time()) if now is None else now
    age = current_time - auth_date
    if age < -30:
        raise TelegramInitDataError("Telegram initData auth_date is in the future.")
    if max_age_seconds > 0 and age > max_age_seconds:
        raise TelegramInitDataError("Telegram initData has expired.")

    try:
        user = json.loads(fields["user"])
        user_id = int(user["id"])
        first_name = str(user["first_name"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("Telegram initData user is invalid.") from exc

    return TelegramInitData(
        user_id=user_id,
        username=str(user["username"]) if user.get("username") else None,
        first_name=first_name,
        last_name=str(user["last_name"]) if user.get("last_name") else None,
        auth_date=auth_date,
        query_id=fields.get("query_id") or None,
    )
