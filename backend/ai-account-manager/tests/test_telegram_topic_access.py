from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_account_manager.auth import telegram_topic_access
from ai_account_manager.auth.telegram_topic_access import (
    TelegramTopicAccessError,
    verify_forum_member_access,
)


@pytest.mark.parametrize(
    ("result", "allowed"),
    [
        ({"status": "creator"}, True),
        ({"status": "administrator"}, True),
        ({"status": "member"}, True),
        ({"status": "restricted", "is_member": True}, True),
        ({"status": "restricted", "is_member": False}, False),
        ({"status": "left"}, False),
        ({"status": "kicked"}, False),
    ],
)
def test_forum_access_uses_cus_bot_membership(
    result: dict[str, object],
    allowed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, int]]] = []

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"ok": True, "result": result}

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, int]) -> Response:
            requests.append((url, json))
            return Response()

    monkeypatch.setattr(
        telegram_topic_access.httpx,
        "AsyncClient",
        lambda **_kwargs: Client(),
    )

    async def scenario() -> None:
        if allowed:
            await verify_forum_member_access(
                bot_token="999:cus-secret",
                chat_id=-1001,
                user_id=777001,
            )
        else:
            with pytest.raises(TelegramTopicAccessError) as failure:
                await verify_forum_member_access(
                    bot_token="999:cus-secret",
                    chat_id=-1001,
                    user_id=777001,
                )
            assert failure.value.code == "TELEGRAM_TOPIC_FORBIDDEN"

    asyncio.run(scenario())
    assert requests == [
        (
            "https://api.telegram.org/bot999:cus-secret/getChatMember",
            {"chat_id": -1001, "user_id": 777001},
        )
    ]


def test_forum_access_fails_closed_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> None:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(
        telegram_topic_access.httpx,
        "AsyncClient",
        lambda **_kwargs: Client(),
    )

    async def scenario() -> None:
        with pytest.raises(TelegramTopicAccessError) as failure:
            await verify_forum_member_access(
                bot_token="999:cus-secret",
                chat_id=-1001,
                user_id=777001,
            )
        assert failure.value.retryable is True

    asyncio.run(scenario())
