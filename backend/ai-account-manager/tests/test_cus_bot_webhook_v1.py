from __future__ import annotations

import json
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.runtime_onboarding import (
    issue_topic_onboarding_token,
    mark_setup_notice_delivered,
)
from ai_account_manager.core.runtime_control import (
    read_runtime_control_store,
    register_agent_instance,
)
from ai_account_manager.integrations.cus_bot import CusBotDeliveryError
from ai_account_manager.integrations import cus_bot_onboarding
from ai_account_manager.integrations.runtime_reconcile_service import reconcile_unbound_topics_once
from ai_account_manager.schemas.runtime_control import AgentInstanceRecord


def _agent() -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id="agent-1",
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity="working-agent-bot",
        telegram_bot_token_fingerprint="sha256:" + "1" * 64,
        forum_chat_id=-1001,
        runtime_socket="/run/user/1000/ai-agent-runtime/agent-1/runtime.sock",
    )


def _update(*, chat_id: int = -1001, is_forum: bool | None = True) -> dict[str, object]:
    chat: dict[str, object] = {"id": chat_id, "type": "supergroup"}
    if is_forum is not None:
        chat["is_forum"] = is_forum
    return {
        "update_id": 7001,
        "message": {
            "message_id": 55,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": chat,
            "message_thread_id": 42,
            "forum_topic_created": {
                "name": "Project Alpha",
                "icon_color": 7322096,
            },
        },
    }


def _configure(monkeypatch, path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("CUS_BOT_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_KEY", "k" * 32)
    monkeypatch.setenv(
        "CUS_MINIAPP_DEEP_LINK_BASE",
        "https://t.me/cus_bot/cus_app?startapp=",
    )
    monkeypatch.setenv("CUS_ALLOWED_FORUM_CHAT_IDS", "-1001")
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_TTL_SECONDS", "1800")
    get_settings.cache_clear()


def test_topic_created_returns_exact_thread_setup_message_and_is_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    deliveries: list[dict[str, object]] = []

    async def fake_send(**kwargs: object) -> int:
        deliveries.append(kwargs)
        return 901

    monkeypatch.setattr(cus_bot_onboarding, "send_topic_notification", fake_send)
    try:
        client = TestClient(create_app())
        headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
        first = client.post(
            "/telegram/cus-bot/webhook",
            headers=headers,
            json=_update(),
        )
        second = client.post(
            "/telegram/cus-bot/webhook",
            headers=headers,
            json=_update(),
        )
        assert first.status_code == 200
        assert second.json() == first.json()
        assert first.json() == {}
        assert len(deliveries) == 1
        payload = deliveries[0]
        assert payload["chat_id"] == -1001
        assert payload["thread_id"] == 42
        assert payload["text"] == "Проект ещё не настроен"
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        assert button["text"] == "Настроить в ЦУС"
        deep_link = button["url"]
        assert deep_link.startswith("https://t.me/cus_bot/cus_app?startapp=")
        token = parse_qs(urlparse(deep_link).query)["startapp"][0]
        persisted = path.read_text(encoding="utf-8")
        assert token not in persisted
        store = read_runtime_control_store(path)
        assert len(store.onboarding_records) == 1
        record = next(iter(store.onboarding_records.values()))
        assert record.chat_id == -1001
        assert record.thread_id == 42
        assert record.topic_name == "Project Alpha"
        assert record.setup_message_id == 901
        assert record.setup_notified_at is not None
    finally:
        get_settings.cache_clear()


def test_expired_setup_button_is_edited_in_place_and_retried_after_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    issued_at = datetime.now(UTC) - timedelta(hours=1)
    issued = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project Alpha",
        ttl_seconds=1800,
        now=issued_at,
        token_factory=lambda: "original-" + "x" * 40,
    )
    mark_setup_notice_delivered(
        path,
        onboarding_id=issued.record.onboarding_id,
        token=issued.token,
        message_id=901,
        now=issued_at,
    )
    edits: list[dict[str, object]] = []

    async def flaky_edit(**kwargs: object) -> None:
        edits.append(kwargs)
        if len(edits) == 1:
            raise CusBotDeliveryError("RETRY", "temporary", retryable=True)

    monkeypatch.setattr(cus_bot_onboarding, "replace_setup_button", flaky_edit)
    try:
        settings = get_settings()
        try:
            asyncio.run(
                cus_bot_onboarding.ensure_topic_setup_notice(
                    settings,
                    chat_id=-1001,
                    thread_id=42,
                    topic_name="Project Alpha",
                )
            )
        except CusBotDeliveryError:
            pass
        else:
            raise AssertionError("first Telegram edit should fail")
        assert len(read_runtime_control_store(path).onboarding_records) == 1
        asyncio.run(
            cus_bot_onboarding.ensure_topic_setup_notice(
                settings,
                chat_id=-1001,
                thread_id=42,
                topic_name="Project Alpha",
            )
        )
        assert edits[1]["deep_link"] == edits[0]["deep_link"]
        record = read_runtime_control_store(path).onboarding_records[issued.record.onboarding_id]
        assert record.setup_message_id == 901
        assert record.setup_button_token_hash == record.token_hash
        assert read_runtime_control_store(path).bindings == {}
    finally:
        get_settings.cache_clear()


def test_webhook_secret_and_forum_allowlist_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    try:
        client = TestClient(create_app())
        denied = client.post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json=_update(),
        )
        assert denied.status_code == 401

        ignored = client.post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json=_update(chat_id=-1002),
        )
        assert ignored.status_code == 200
        assert ignored.json() == {}
        assert read_runtime_control_store(path).onboarding_records == {}

        explicit_non_forum = client.post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json=_update(is_forum=False),
        )
        assert explicit_non_forum.status_code == 200
        assert explicit_non_forum.json() == {}
        assert read_runtime_control_store(path).onboarding_records == {}
    finally:
        get_settings.cache_clear()


def test_optional_forum_flag_may_be_absent_from_topic_created_update(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    deliveries: list[dict[str, object]] = []

    async def fake_send(**kwargs: object) -> int:
        deliveries.append(kwargs)
        return 902

    monkeypatch.setattr(cus_bot_onboarding, "send_topic_notification", fake_send)
    try:
        client = TestClient(create_app())
        response = client.post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json=_update(is_forum=None),
        )
        assert response.status_code == 200
        assert response.json() == {}
        assert len(deliveries) == 1
        assert deliveries[0]["thread_id"] == 42
        assert len(read_runtime_control_store(path).onboarding_records) == 1
    finally:
        get_settings.cache_clear()


def test_setup_notice_retries_after_confirmed_delivery_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    attempts = 0

    async def flaky_send(**_kwargs: object) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CusBotDeliveryError(
                "CUS_BOT_DELIVERY_UNAVAILABLE",
                "Telegram delivery failed",
                retryable=True,
            )
        return 903

    monkeypatch.setattr(cus_bot_onboarding, "send_topic_notification", flaky_send)
    try:
        client = TestClient(create_app())
        headers = {"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"}
        first = client.post("/telegram/cus-bot/webhook", headers=headers, json=_update())
        assert first.status_code == 503
        record = next(iter(read_runtime_control_store(path).onboarding_records.values()))
        assert record.setup_message_id is None

        second = client.post("/telegram/cus-bot/webhook", headers=headers, json=_update())
        third = client.post("/telegram/cus-bot/webhook", headers=headers, json=_update())
        assert second.status_code == third.status_code == 200
        assert attempts == 2
        record = next(iter(read_runtime_control_store(path).onboarding_records.values()))
        assert record.setup_message_id == 903
    finally:
        get_settings.cache_clear()


def test_non_topic_update_is_ignored_without_state_write(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    before = json.loads(path.read_text(encoding="utf-8"))["store_revision"]
    _configure(monkeypatch, path)
    try:
        client = TestClient(create_app())
        response = client.post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json={"update_id": 7002, "message": {"text": "ordinary message"}},
        )
        assert response.status_code == 200
        assert response.json() == {}
        after = json.loads(path.read_text(encoding="utf-8"))["store_revision"]
        assert after == before
    finally:
        get_settings.cache_clear()


def test_runtime_discovery_recovers_missed_webhook_without_duplicate_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    _configure(monkeypatch, path)
    deliveries: list[dict[str, object]] = []

    async def fake_send(**kwargs: object) -> int:
        deliveries.append(kwargs)
        return 915

    class FakeConnector:
        async def status(self) -> dict[str, object]:
            return {
                "status": {
                    "agent_instance_id": "agent-1",
                    "topic_discovery": {
                        "status": "OK",
                        "unbound_topics": [{"thread_id": 151, "topic_name": "CUS Auto QA 2"}],
                    },
                }
            }

    monkeypatch.setattr(cus_bot_onboarding, "send_topic_notification", fake_send)
    try:
        settings = get_settings()
        first = asyncio.run(
            reconcile_unbound_topics_once(
                settings,
                agent_instance_id="agent-1",
                connector=FakeConnector(),  # type: ignore[arg-type]
            )
        )
        second = asyncio.run(
            reconcile_unbound_topics_once(
                settings,
                agent_instance_id="agent-1",
                connector=FakeConnector(),  # type: ignore[arg-type]
            )
        )
        assert first == 1
        assert second == 0
        assert len(deliveries) == 1
        assert deliveries[0]["chat_id"] == -1001
        assert deliveries[0]["thread_id"] == 151
        record = next(iter(read_runtime_control_store(path).onboarding_records.values()))
        assert record.thread_id == 151
        assert record.setup_message_id == 915

        # A delayed real Telegram topic-created update must not send again.
        update = _update()
        update["message"]["message_thread_id"] = 151  # type: ignore[index]
        update["message"]["forum_topic_created"]["name"] = "CUS Auto QA 2"  # type: ignore[index]
        response = TestClient(create_app()).post(
            "/telegram/cus-bot/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json=update,
        )
        assert response.status_code == 200
        assert len(deliveries) == 1
    finally:
        get_settings.cache_clear()
