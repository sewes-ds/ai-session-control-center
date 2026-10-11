from __future__ import annotations

import asyncio

import pytest

from ai_account_manager.integrations import cus_bot
from ai_account_manager.integrations.cus_bot import (
    CusBotDeliveryError,
    send_topic_notification,
)


def test_completion_delivery_requires_exact_topic_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "ok": True,
                "result": {
                    "message_id": 9001,
                    "message_thread_id": 42,
                    "chat": {"id": -1001},
                },
            }

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, object]) -> Response:
            requests.append(json)
            return Response()

    monkeypatch.setattr(cus_bot.httpx, "AsyncClient", lambda **_kwargs: Client())

    async def scenario() -> None:
        message_id = await send_topic_notification(
            bot_token="999:cus-token",
            chat_id=-1001,
            thread_id=42,
            text="Настройка завершена. Можно начинать работу",
        )
        assert message_id == 9001

    asyncio.run(scenario())
    assert requests == [
        {
            "chat_id": -1001,
            "message_thread_id": 42,
            "text": "Настройка завершена. Можно начинать работу",
        }
    ]


def test_completion_delivery_rejects_cross_topic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "ok": True,
                "result": {
                    "message_id": 9001,
                    "message_thread_id": 99,
                    "chat": {"id": -1001},
                },
            }

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, object]) -> Response:
            return Response()

    monkeypatch.setattr(cus_bot.httpx, "AsyncClient", lambda **_kwargs: Client())

    async def scenario() -> None:
        with pytest.raises(CusBotDeliveryError) as failure:
            await send_topic_notification(
                bot_token="999:cus-token",
                chat_id=-1001,
                thread_id=42,
                text="done",
            )
        assert failure.value.code == "CUS_BOT_DELIVERY_MISMATCH"
        assert failure.value.retryable is False

    asyncio.run(scenario())
