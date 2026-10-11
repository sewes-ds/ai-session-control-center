from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.runtime_control import (
    read_runtime_control_store,
    register_agent_instance,
    upsert_runtime_binding,
)
from ai_account_manager.core.runtime_onboarding import (
    RuntimeOnboardingError,
    finalize_topic_onboarding,
    issue_topic_onboarding_token,
    mark_setup_notice_delivered,
    prepare_topic_onboarding_binding,
    renew_expired_topic_onboarding_token,
    renew_binding_onboarding_token,
    resolve_topic_onboarding_token,
)
from ai_account_manager.integrations.ai_agent_runtime import RuntimeConnectorError
from ai_account_manager.integrations.cus_bot import CusBotDeliveryError
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    RuntimeBindingRecord,
    canonical_digest,
)

BOT_TOKEN = "123456:cus-bot-token"


def _agent() -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id="agent-1",
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity="cus-working-bot",
        telegram_bot_token_fingerprint="sha256:" + "1" * 64,
        forum_chat_id=-1001,
        runtime_socket="/run/user/1000/ai-agent-runtime/agent-1/runtime.sock",
    )


def _binding() -> RuntimeBindingRecord:
    desired_payload: dict[str, object] = {
        "binding_id": "binding-1",
        "agent_instance_id": "agent-1",
        "generation_id": "gen-1",
        "revision": 1,
        "project_id": "project-1",
        "canonical_cwd": "/srv/project",
        "provider": "codex",
        "account_profile": "work",
        "model": "gpt-test",
        "reasoning_effort": "high",
    }
    return RuntimeBindingRecord(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        project_id="project-1",
        canonical_cwd="/srv/project",
        desired=DesiredSpecRecord(
            **desired_payload,
            spec_digest=canonical_digest(desired_payload),
            source_kind="cus_ui",
            source_ref="user:1",
        ),
    )


def _signed_init_data(*, auth_date: int, bot_token: str = BOT_TOKEN) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-onboarding",
        "user": json.dumps(
            {"id": 777001, "first_name": "Eugene"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_topic_token_is_scoped_hashed_and_rotatable(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    now = datetime(2026, 10, 3, tzinfo=UTC)
    first_token = "a" * 40
    first = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=1800,
        now=now,
        token_factory=lambda: first_token,
    )
    persisted = path.read_text(encoding="utf-8")
    assert first_token not in persisted
    assert first.record.agent_instance_id == "agent-1"
    assert resolve_topic_onboarding_token(path, first_token, now=now) == first.record

    second_token = "b" * 40
    second = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Renamed project",
        ttl_seconds=1800,
        now=now + timedelta(minutes=1),
        token_factory=lambda: second_token,
    )
    assert second.record.onboarding_id == first.record.onboarding_id
    assert second.record.topic_name == "Renamed project"
    with pytest.raises(RuntimeOnboardingError) as old:
        resolve_topic_onboarding_token(path, first_token, now=now + timedelta(minutes=1))
    assert old.value.code == "ONBOARDING_TOKEN_INVALID"


def test_token_expiry_and_existing_binding_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    now = datetime(2026, 10, 3, tzinfo=UTC)
    token = "c" * 40
    issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=60,
        now=now,
        token_factory=lambda: token,
    )
    with pytest.raises(RuntimeOnboardingError) as expired:
        resolve_topic_onboarding_token(path, token, now=now + timedelta(seconds=60))
    assert expired.value.code == "ONBOARDING_TOKEN_EXPIRED"

    upsert_runtime_binding(path, _binding())
    with pytest.raises(RuntimeOnboardingError) as bound:
        issue_topic_onboarding_token(
            path,
            chat_id=-1001,
            thread_id=42,
            topic_name="Project",
            ttl_seconds=60,
        )
    assert bound.value.code == "TOPIC_ALREADY_BOUND"


def test_expired_delivered_setup_link_rotates_in_place_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    now = datetime(2026, 10, 3, tzinfo=UTC)
    original = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=60,
        now=now,
        token_factory=lambda: "old-token-" + "a" * 40,
    )
    mark_setup_notice_delivered(
        path,
        onboarding_id=original.record.onboarding_id,
        token=original.token,
        message_id=123,
        now=now,
    )
    first = renew_expired_topic_onboarding_token(
        path,
        old_token=original.token,
        token_key="k" * 32,
        ttl_seconds=60,
        now=now + timedelta(seconds=61),
    )
    assert first.record.setup_message_id == 123
    assert first.record.onboarding_id == original.record.onboarding_id
    assert first.token not in path.read_text(encoding="utf-8")
    revision = read_runtime_control_store(path).store_revision
    replay = renew_expired_topic_onboarding_token(
        path,
        old_token=original.token,
        token_key="k" * 32,
        ttl_seconds=60,
        now=now + timedelta(seconds=62),
    )
    assert replay.token == first.token
    assert read_runtime_control_store(path).store_revision == revision
    assert read_runtime_control_store(path).bindings == {}
    assert resolve_topic_onboarding_token(path, first.token, now=now + timedelta(seconds=62))


def test_unknown_forum_cannot_create_onboarding(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    with pytest.raises(RuntimeOnboardingError) as failure:
        issue_topic_onboarding_token(
            path,
            chat_id=-1002,
            thread_id=42,
            topic_name="Project",
            ttl_seconds=60,
        )
    assert failure.value.code == "AGENT_INSTANCE_AMBIGUOUS"


def test_prepare_then_finalize_is_durable_atomic_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    now = datetime(2026, 10, 3, tzinfo=UTC)
    issued = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=1800,
        now=now,
        token_factory=lambda: "d" * 40,
    )
    prepared = prepare_topic_onboarding_binding(
        path,
        onboarding_id=issued.record.onboarding_id,
        token=issued.token,
        project_id="project-1",
        canonical_cwd="/srv/project",
        provider="codex",
        account_profile="work",
        model="gpt-test",
        reasoning_effort="high",
        source_ref="telegram-user:777001",
        now=now,
        generation_factory=lambda: "gen-test",
    )
    before = read_runtime_control_store(path)
    assert before.bindings[prepared.binding.binding_id].lifecycle_status == "UNCONFIGURED"
    assert before.onboarding_records[issued.record.onboarding_id].state == "UNCONFIGURED"

    renewed = renew_binding_onboarding_token(
        path,
        binding_id=prepared.binding.binding_id,
        ttl_seconds=1800,
        now=now + timedelta(milliseconds=500),
        token_factory=lambda: "e" * 40,
    )
    assert renewed.record.binding_id == prepared.binding.binding_id
    assert renewed.record.onboarding_id == issued.record.onboarding_id
    with pytest.raises(RuntimeOnboardingError) as rotated:
        resolve_topic_onboarding_token(path, issued.token, now=now + timedelta(seconds=1))
    assert rotated.value.code == "ONBOARDING_TOKEN_INVALID"

    record, binding = finalize_topic_onboarding(
        path,
        onboarding_id=issued.record.onboarding_id,
        binding_id=prepared.binding.binding_id,
        expected_spec_digest=prepared.binding.desired.spec_digest,
        now=now + timedelta(seconds=1),
    )
    assert record.state == "CONFIGURED"
    assert binding.lifecycle_status == "CONFIGURED"
    replay_record, replay_binding = finalize_topic_onboarding(
        path,
        onboarding_id=issued.record.onboarding_id,
        binding_id=prepared.binding.binding_id,
        expected_spec_digest=prepared.binding.desired.spec_digest,
        now=now + timedelta(seconds=2),
    )
    assert replay_record.configured_at == record.configured_at
    assert replay_binding.desired.spec_digest == binding.desired.spec_digest


def test_onboarding_api_requires_telegram_auth_even_with_preview_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    issued = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=1800,
    )
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:working-agent-token")
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777001")
    monkeypatch.setenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600")
    monkeypatch.setenv("CUS_PREVIEW_TOKEN", "preview")
    get_settings.cache_clear()
    try:

        async def allow_member(**kwargs: object) -> None:
            assert kwargs == {
                "bot_token": BOT_TOKEN,
                "chat_id": -1001,
                "user_id": 777001,
            }

        monkeypatch.setattr(
            "ai_account_manager.api.routes.onboarding.verify_forum_member_access",
            allow_member,
        )
        client = TestClient(create_app())
        preview = client.get(
            f"/api/onboarding/{issued.token}",
            headers={"X-CUS-Preview-Token": "preview"},
        )
        assert preview.status_code == 401

        now = int(datetime.now(UTC).timestamp())
        response = client.get(
            f"/api/onboarding/{issued.token}",
            headers={"X-Telegram-Init-Data": _signed_init_data(auth_date=now)},
        )
        assert response.status_code == 200
        assert response.json()["thread_id"] == 42
        assert response.json()["agent_instance_id"] == "agent-1"
        assert response.json()["completion_notified"] is False

        wrong_bot = client.get(
            f"/api/onboarding/{issued.token}",
            headers={
                "X-Telegram-Init-Data": _signed_init_data(
                    auth_date=now,
                    bot_token="111111:working-agent-token",
                )
            },
        )
        assert wrong_bot.status_code == 401
    finally:
        get_settings.cache_clear()


def test_expired_setup_link_refreshes_existing_bot_button_after_member_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    issued_at = datetime.now(UTC) - timedelta(minutes=2)
    issued = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=60,
        now=issued_at,
        token_factory=lambda: "initial-" + "b" * 40,
    )
    mark_setup_notice_delivered(
        path,
        onboarding_id=issued.record.onboarding_id,
        token=issued.token,
        message_id=123,
        now=issued_at,
    )
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777001")
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_KEY", "k" * 32)
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_TTL_SECONDS", "1800")
    monkeypatch.setenv("CUS_MINIAPP_DEEP_LINK_BASE", "https://t.me/test_bot/app?startapp=")
    get_settings.cache_clear()
    edits: list[dict[str, object]] = []

    async def allow_member(**kwargs: object) -> None:
        assert kwargs["chat_id"] == -1001
        assert kwargs["user_id"] == 777001

    async def edit_button(**kwargs: object) -> None:
        edits.append(kwargs)

    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.verify_forum_member_access", allow_member
    )
    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.replace_setup_button", edit_button
    )
    try:
        client = TestClient(create_app())
        url = f"/api/onboarding/{issued.token}"
        assert client.get(url).status_code == 401
        assert (
            read_runtime_control_store(path)
            .onboarding_records[issued.record.onboarding_id]
            .token_hash
            == issued.record.token_hash
        )
        headers = {
            "X-Telegram-Init-Data": _signed_init_data(auth_date=int(datetime.now(UTC).timestamp()))
        }
        first = client.get(url, headers=headers)
        assert first.status_code == 410
        assert first.json()["detail"]["code"] == "ONBOARDING_TOKEN_EXPIRED"
        assert edits[0]["chat_id"] == -1001
        assert edits[0]["message_id"] == 123
        assert "startapp=" in str(edits[0]["deep_link"])
        client.get(url, headers=headers)
        assert edits[1]["deep_link"] == edits[0]["deep_link"]
        assert read_runtime_control_store(path).bindings == {}
    finally:
        get_settings.cache_clear()


def test_configured_binding_selection_is_topic_scoped_and_never_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent())
    first = _binding()
    second = first.model_copy(update={"binding_id": "binding-2", "thread_id": 43})
    second_payload = {**first.desired.digest_payload(), "binding_id": "binding-2"}
    second = second.model_copy(
        update={
            "desired": DesiredSpecRecord(
                **second_payload,
                spec_digest=canonical_digest(second_payload),
                source_kind="cus_ui",
                source_ref="user:1",
            )
        }
    )
    upsert_runtime_binding(path, first)
    upsert_runtime_binding(path, second)
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777001")
    monkeypatch.setenv("CUS_PREVIEW_TOKEN", "preview")
    get_settings.cache_clear()

    async def allow_member(**kwargs: object) -> None:
        assert kwargs["chat_id"] == -1001
        assert kwargs["user_id"] == 777001

    calls: list[tuple[str, int]] = []

    class FakeConnector:
        def __init__(self, _socket_path: Path, *, timeout: float) -> None:
            assert isinstance(_socket_path, Path)
            assert timeout == 10.0

        async def apply_binding(
            self,
            binding: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, object]:
            assert request_id is not None
            calls.append((binding.binding_id, binding.desired.revision))
            return {"ok": True}

    monkeypatch.setattr(
        "ai_account_manager.api.routes.runtime.verify_forum_member_access",
        allow_member,
    )
    monkeypatch.setattr(
        "ai_account_manager.api.routes.runtime.validate_provider_project_settings",
        lambda **_kwargs: SimpleNamespace(status="valid", capability_source="current"),
    )
    monkeypatch.setattr("ai_account_manager.api.routes.runtime.RuntimeConnector", FakeConnector)
    payload = {
        "expected_generation_id": first.desired.generation_id,
        "expected_revision": first.desired.revision,
        "expected_spec_digest": first.desired.spec_digest,
        "account_profile": "backup",
        "model": "gpt-test-2",
        "reasoning_effort": "medium",
    }
    try:
        client = TestClient(create_app())
        preview = client.post(
            "/api/runtime/bindings/binding-1/selection",
            headers={"X-CUS-Preview-Token": "preview"},
            json=payload,
        )
        assert preview.status_code == 401
        headers = {
            "X-Telegram-Init-Data": _signed_init_data(auth_date=int(datetime.now(UTC).timestamp()))
        }
        accepted = client.post(
            "/api/runtime/bindings/binding-1/selection", headers=headers, json=payload
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "pending_idle_or_launch"
        assert calls == [("binding-1", 2)]
        stored = read_runtime_control_store(path)
        assert stored.bindings["binding-1"].desired.account_profile == "backup"
        assert stored.bindings["binding-2"].desired.account_profile == "work"
        stale = client.post(
            "/api/runtime/bindings/binding-1/selection", headers=headers, json=payload
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "desired_revision_conflict"
    finally:
        get_settings.cache_clear()


def test_onboarding_api_reports_success_only_after_runtime_ack_and_retries_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-control.json"
    project = tmp_path / "projects" / "alpha"
    project.mkdir(parents=True)
    register_agent_instance(path, _agent())
    issued = issue_topic_onboarding_token(
        path,
        chat_id=-1001,
        thread_id=42,
        topic_name="Project",
        ttl_seconds=1800,
    )
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("AI_PROJECT_ROOTS", str(tmp_path / "projects"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:working-agent-token")
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "777001")
    monkeypatch.setenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "3600")
    get_settings.cache_clear()
    calls: list[tuple[int, str | None]] = []
    notifications: list[dict[str, object]] = []
    notification_failures_remaining = 1

    async def allow_member(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.verify_forum_member_access",
        allow_member,
    )
    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.validate_provider_project_settings",
        lambda **_kwargs: None,
    )

    class FakeConnector:
        fail = True

        def __init__(self, socket_path: Path, *, timeout: float) -> None:
            assert socket_path == Path("/run/user/1000/ai-agent-runtime/agent-1/runtime.sock")
            assert timeout == 10.0

        async def apply_binding(
            self,
            binding: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, object]:
            calls.append((binding.desired.revision, request_id))
            if self.fail:
                raise RuntimeConnectorError(
                    "RUNTIME_UNAVAILABLE",
                    "Runtime unavailable",
                    retryable=True,
                )
            return {"ok": True}

    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.RuntimeConnector",
        FakeConnector,
    )

    async def notify(**kwargs: object) -> int:
        nonlocal notification_failures_remaining
        notifications.append(kwargs)
        if notification_failures_remaining:
            notification_failures_remaining -= 1
            raise CusBotDeliveryError(
                "CUS_BOT_DELIVERY_UNAVAILABLE",
                "CUS Bot notification delivery is unavailable",
                retryable=True,
            )
        return 9001

    monkeypatch.setattr(
        "ai_account_manager.api.routes.onboarding.send_topic_notification",
        notify,
    )
    now = int(datetime.now(UTC).timestamp())
    headers = {"X-Telegram-Init-Data": _signed_init_data(auth_date=now)}
    payload = {
        "onboarding_id": issued.record.onboarding_id,
        "project_path": str(project),
        "provider": "codex",
        "account_profile": "work",
        "model": "gpt-test",
        "reasoning_effort": "high",
    }
    try:
        client = TestClient(create_app())
        failed = client.post(
            f"/api/onboarding/{issued.token}/apply",
            headers=headers,
            json=payload,
        )
        assert failed.status_code == 503
        interim = read_runtime_control_store(path)
        binding = next(iter(interim.bindings.values()))
        assert binding.lifecycle_status == "UNCONFIGURED"
        assert interim.onboarding_records[issued.record.onboarding_id].state == "UNCONFIGURED"
        assert notifications == []

        FakeConnector.fail = False
        notification_failed = client.post(
            f"/api/onboarding/{issued.token}/apply",
            headers=headers,
            json=payload,
        )
        assert notification_failed.status_code == 503
        configured_without_notice = read_runtime_control_store(path)
        configured_record = configured_without_notice.onboarding_records[
            issued.record.onboarding_id
        ]
        assert configured_record.state == "CONFIGURED"
        assert configured_record.completion_message_id is None
        assert len(calls) == 2
        assert len(notifications) == 1

        configured_status = client.get(
            f"/api/onboarding/{issued.token}",
            headers=headers,
        )
        assert configured_status.status_code == 200
        assert configured_status.json()["state"] == "CONFIGURED"
        assert configured_status.json()["completion_notified"] is False

        succeeded = client.post(
            f"/api/onboarding/{issued.token}/complete",
            headers=headers,
        )
        assert succeeded.status_code == 200
        assert succeeded.json()["status"] == "CONFIGURED"
        assert succeeded.json()["close_miniapp"] is True
        assert calls[0] == calls[1]
        assert calls[1][0] == 1
        assert notifications == 2 * [
            {
                "bot_token": BOT_TOKEN,
                "chat_id": -1001,
                "thread_id": 42,
                "text": "Настройка завершена. Можно начинать работу",
                "timeout_seconds": 10.0,
            }
        ]
        completed = read_runtime_control_store(path).onboarding_records[issued.record.onboarding_id]
        assert completed.completion_message_id == 9001

        completed_status = client.get(
            f"/api/onboarding/{issued.token}",
            headers=headers,
        )
        assert completed_status.status_code == 200
        assert completed_status.json()["completion_notified"] is True

        replay = client.post(
            f"/api/onboarding/{issued.token}/apply",
            headers=headers,
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json() == succeeded.json()
        assert len(calls) == 2
        assert len(notifications) == 2
    finally:
        get_settings.cache_clear()
