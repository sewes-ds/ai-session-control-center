from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_account_manager import app as app_module
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.runtime_control import (
    prepare_project_relocation,
    read_runtime_control_store,
    register_agent_instance,
    upsert_runtime_binding,
)
from ai_account_manager.core.runtime_onboarding import resolve_topic_onboarding_token
from ai_account_manager.integrations.ai_agent_runtime import RuntimeConnectorError
from ai_account_manager.integrations import runtime_reconcile_service
from ai_account_manager.integrations.runtime_reconcile_service import (
    observed_policy,
    notification_sender,
    reconcile_all_runtime_instances_once,
    reconcile_desired_state_once,
    reconcile_pending_project_relocations,
)
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    ObservedSpecProposalRecord,
    RuntimeBindingRecord,
    RuntimeUserNotificationRecord,
    canonical_digest,
)


def _agent(agent_id: str, chat_id: int, digit: str) -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id=agent_id,
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity=f"bot-{agent_id}",
        telegram_bot_token_fingerprint="sha256:" + digit * 64,
        forum_chat_id=chat_id,
        runtime_socket=f"/run/user/1000/ai-agent-runtime/{agent_id}/runtime.sock",
    )


def _binding_and_proposal() -> tuple[RuntimeBindingRecord, ObservedSpecProposalRecord]:
    desired_payload: dict[str, object] = {
        "binding_id": "binding-1",
        "agent_instance_id": "agent-1",
        "generation_id": "gen-1",
        "revision": 1,
        "project_id": "project-1",
        "canonical_cwd": "/srv/project",
        "provider": "codex",
        "account_profile": "work",
        "model": "gpt-old",
        "reasoning_effort": "medium",
    }
    binding = RuntimeBindingRecord(
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
    proposal = ObservedSpecProposalRecord(
        binding_id=binding.binding_id,
        base_generation_id=binding.desired.generation_id,
        base_revision=binding.desired.revision,
        base_spec_digest=binding.desired.spec_digest,
        model="gpt-new",
        reasoning_effort="high",
        instance_id="instance-1",
        observation_sequence=1,
        evidence_digest="sha256:" + "2" * 64,
    )
    return binding, proposal


def test_reconcile_cycle_isolates_runtime_outages_per_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent("agent-1", -1001, "1"))
    register_agent_instance(path, _agent("agent-2", -1002, "2"))
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("RUNTIME_RECONCILE_BATCH_SIZE", "17")
    get_settings.cache_clear()
    calls: list[tuple[Path, float, int]] = []
    heartbeats: list[str] = []

    class FakeConnector:
        def __init__(self, socket_path: Path, *, timeout: float) -> None:
            self.socket_path = socket_path
            self.timeout = timeout

        async def control_plane_heartbeat(self, agent_instance_id: str) -> dict[str, Any]:
            heartbeats.append(agent_instance_id)
            if "agent-2" in str(self.socket_path):
                raise RuntimeConnectorError(
                    "RUNTIME_UNAVAILABLE",
                    "Runtime is unavailable",
                    retryable=True,
                )
            return {"ok": True, "sync_state": "IN_SYNC"}

        async def pull_outbox(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
            calls.append((self.socket_path, self.timeout, limit))
            return ()

        async def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
            raise AssertionError("No events should be acknowledged")

    try:
        cycle = asyncio.run(
            reconcile_all_runtime_instances_once(
                get_settings(),
                connector_factory=FakeConnector,  # type: ignore[arg-type]
            )
        )
    finally:
        get_settings.cache_clear()

    assert [result.agent_instance_id for result in cycle.agents] == ["agent-1", "agent-2"]
    assert cycle.agents[0].status == "OK"
    assert cycle.agents[0].report is not None
    assert cycle.agents[1].status == "DEGRADED"
    assert cycle.agents[1].error_code == "RUNTIME_UNAVAILABLE"
    assert cycle.healthy is False
    assert heartbeats == ["agent-1", "agent-2"]
    assert len(calls) == 1
    assert all(limit == 17 for _socket, _timeout, limit in calls)


def test_observed_policy_uses_provider_adapter_validation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    binding, proposal = _binding_and_proposal()
    captured: list[dict[str, object]] = []

    def validate(**kwargs: object) -> object:
        captured.append(kwargs)
        return SimpleNamespace(status="valid")

    monkeypatch.setattr(runtime_reconcile_service, "validate_provider_project_settings", validate)
    policy = observed_policy(get_settings())
    assert policy(binding, proposal) is True
    assert captured[0]["provider_id"] == "codex"
    assert captured[0]["account_profile"] == "work"
    assert captured[0]["model"] == "gpt-new"
    assert captured[0]["reasoning_effort"] == "high"


def test_reconciler_retries_full_project_relocation_snapshot_and_persists_ack(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent("agent-1", -1001, "1"))
    binding, _proposal = _binding_and_proposal()
    upsert_runtime_binding(path, binding)
    _store, relocation = prepare_project_relocation(
        path,
        project_id="project-1",
        old_canonical_cwd="/srv/project",
        new_canonical_cwd="/srv/project-moved",
        relocation_id="rel_" + "3" * 32,
    )
    calls: list[dict[str, object]] = []

    class FakeConnector:
        async def apply_snapshot(self, **kwargs: object) -> dict[str, Any]:
            calls.append(kwargs)
            return {"ok": True}

    applied = asyncio.run(
        reconcile_pending_project_relocations(
            store_path=path,
            agent_instance_id="agent-1",
            connector=FakeConnector(),  # type: ignore[arg-type]
        )
    )
    assert applied == 1
    assert len(calls) == 1
    assert calls[0]["generation_id"] == "gen-1"
    assert calls[0]["project_relocations"] == (relocation,)
    request_id = calls[0]["request_id"]
    assert isinstance(request_id, str) and request_id.startswith("relocation-apply:")
    persisted = read_runtime_control_store(path).project_relocations[relocation.relocation_id]
    assert persisted.complete is True

    replayed = asyncio.run(
        reconcile_pending_project_relocations(
            store_path=path,
            agent_instance_id="agent-1",
            connector=FakeConnector(),  # type: ignore[arg-type]
        )
    )
    assert replayed == 0
    assert len(calls) == 1


def test_periodic_reconcile_repairs_missed_desired_push(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent("agent-1", -1001, "1"))
    binding, _proposal = _binding_and_proposal()
    upsert_runtime_binding(path, binding)
    applied: list[tuple[RuntimeBindingRecord, str | None]] = []

    class FakeConnector:
        async def status(self) -> dict[str, Any]:
            return {
                "ok": True,
                "status": {"agent_instance_id": "agent-1", "bindings": []},
            }

        async def apply_binding(
            self,
            record: RuntimeBindingRecord,
            *,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            applied.append((record, request_id))
            return {"ok": True}

    repaired = asyncio.run(
        reconcile_desired_state_once(
            store_path=path,
            agent_instance_id="agent-1",
            connector=FakeConnector(),  # type: ignore[arg-type]
        )
    )
    assert repaired == 1
    assert applied[0][0] == binding
    assert applied[0][1] == (
        f"desired-apply:{binding.binding_id}:{binding.desired.spec_digest[7:]}"
    )


def test_periodic_reconcile_uses_full_snapshot_for_unmanaged_state(tmp_path: Path) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent("agent-1", -1001, "1"))
    binding, _proposal = _binding_and_proposal()
    unmanaged = binding.model_copy(update={"lifecycle_status": "UNMANAGED"})
    upsert_runtime_binding(path, unmanaged)
    snapshots: list[dict[str, object]] = []

    class FakeConnector:
        async def status(self) -> dict[str, Any]:
            return {
                "ok": True,
                "status": {"agent_instance_id": "agent-1", "bindings": []},
            }

        async def apply_snapshot(self, **kwargs: object) -> dict[str, Any]:
            snapshots.append(kwargs)
            return {"ok": True}

    repaired = asyncio.run(
        reconcile_desired_state_once(
            store_path=path,
            agent_instance_id="agent-1",
            connector=FakeConnector(),  # type: ignore[arg-type]
        )
    )
    assert repaired == 1
    assert snapshots[0]["bindings"] == (unmanaged,)
    assert str(snapshots[0]["request_id"]).startswith("desired-snapshot:")


def test_onboarding_notification_rotates_token_and_sends_exact_topic_button(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "runtime-control.json"
    register_agent_instance(path, _agent("agent-1", -1001, "1"))
    binding, _proposal = _binding_and_proposal()
    unconfigured = binding.model_copy(update={"lifecycle_status": "UNCONFIGURED"})
    upsert_runtime_binding(path, unconfigured)
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(path))
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", "123456:cus-bot")
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_KEY", "test-key")
    monkeypatch.setenv("CUS_MINIAPP_DEEP_LINK_BASE", "https://t.me/cus/app?startapp=")
    get_settings.cache_clear()
    calls: list[dict[str, object]] = []

    async def send(**kwargs: object) -> int:
        calls.append(kwargs)
        return 9001

    monkeypatch.setattr(runtime_reconcile_service, "send_topic_notification", send)
    notification = RuntimeUserNotificationRecord.model_validate(
        {
            "schema": "ai-agent-runtime.user-notification.v1",
            "notification_code": "ONBOARDING_REQUIRED",
            "agent_instance_id": "agent-1",
            "binding_id": "binding-1",
            "chat_id": -1001,
            "thread_id": 42,
        }
    )

    async def scenario() -> None:
        assert await notification_sender(get_settings())(notification) == 9001

    try:
        asyncio.run(scenario())
        assert len(calls) == 1
        assert calls[0]["chat_id"] == -1001
        assert calls[0]["thread_id"] == 42
        markup = calls[0]["reply_markup"]
        assert isinstance(markup, dict)
        button = markup["inline_keyboard"][0][0]  # type: ignore[index]
        assert button["text"] == "Настроить в ЦУС"
        token = str(button["url"]).split("startapp=", 1)[1]
        resolved = resolve_topic_onboarding_token(path, token)
        assert resolved.binding_id == "binding-1"
    finally:
        get_settings.cache_clear()


def test_app_lifespan_starts_reconciler_only_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started = threading.Event()

    async def fake_loop(_settings: object) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setenv("RUNTIME_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("CUS_TELEGRAM_BOT_TOKEN", "test-cus-bot-token")
    monkeypatch.setenv("CUS_BOT_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setenv("CUS_ONBOARDING_TOKEN_KEY", "k" * 32)
    monkeypatch.setenv(
        "CUS_MINIAPP_DEEP_LINK_BASE",
        "https://t.me/cus_bot/cus_app?startapp=",
    )
    monkeypatch.setenv("CUS_ALLOWED_FORUM_CHAT_IDS", "-1001")
    monkeypatch.setattr(app_module, "runtime_reconcile_loop", fake_loop)
    get_settings.cache_clear()
    try:
        with TestClient(app_module.create_app()):
            assert started.wait(timeout=1.0)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RUNTIME_RECONCILE_INTERVAL_SECONDS", "0", "must be greater than"),
        ("RUNTIME_RECONCILE_INTERVAL_SECONDS", "3601", "must be greater than"),
        ("RUNTIME_RECONCILE_BATCH_SIZE", "0", "must be in"),
        ("RUNTIME_RECONCILE_BATCH_SIZE", "501", "must be in"),
    ],
)
def test_runtime_reconcile_settings_are_bounded(
    monkeypatch,
    name: str,
    value: str,
    message: str,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match=message):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_runtime_reconcile_fails_fast_without_separate_cus_bot_trust_boundary(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RUNTIME_RECONCILE_ENABLED", "true")
    monkeypatch.delenv("CUS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CUS_BOT_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("CUS_ONBOARDING_TOKEN_KEY", raising=False)
    monkeypatch.delenv("CUS_MINIAPP_DEEP_LINK_BASE", raising=False)
    monkeypatch.delenv("CUS_ALLOWED_FORUM_CHAT_IDS", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="requires CUS_"):
            get_settings()
    finally:
        get_settings.cache_clear()
