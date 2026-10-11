from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ai_account_manager.app import create_app
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.runtime_control import (
    register_agent_instance,
    upsert_runtime_binding,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.integrations.runtime_health import collect_runtime_binding_health
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    RuntimeBindingRecord,
    canonical_digest,
)


def _agent(agent_id: str = "agent-1", chat_id: int = -1001) -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id=agent_id,
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity=f"bot-{agent_id}",
        telegram_bot_token_fingerprint=f"sha256:{'1' * 64}",
        forum_chat_id=chat_id,
        runtime_socket=f"/run/user/1000/ai-agent-runtime/{agent_id}/runtime.sock",
    )


def _binding() -> RuntimeBindingRecord:
    desired_payload: dict[str, object] = {
        "binding_id": "binding-1",
        "agent_instance_id": "agent-1",
        "generation_id": "gen-1",
        "revision": 3,
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


class _FakeConnector:
    def __init__(self, response: dict[str, Any] | None, error: RuntimeConnectorError | None = None):
        self.response = response
        self.error = error

    async def status(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_public_health_correlates_binding_and_drops_process_internals(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    binding = _binding()
    register_agent_instance(store_path, _agent())
    upsert_runtime_binding(store_path, binding)
    response = {
        "status": {
            "agent_instance_id": "agent-1",
            "bindings": [
                {
                    "binding_id": "binding-1",
                    "source_ref": {
                        "agent_instance_id": "agent-1",
                        "chat_id": -1001,
                        "thread_id": 42,
                    },
                    "health": "PENDING_CHANGE",
                    "management_mode": "MANAGED",
                    "desired": {
                        "generation_id": "gen-1",
                        "revision": 3,
                        "spec_digest": binding.desired.spec_digest,
                        "profile_fingerprint": "must-not-be-public",
                    },
                    "instances": [
                        {
                            "lifecycle": "BUSY",
                            "wrapper_pid": 1234,
                            "child_pid": 1240,
                            "tmux_session": "cc-private",
                            "applied": {
                                "generation_id": "gen-1",
                                "revision": 2,
                                "spec_digest": f"sha256:{'2' * 64}",
                            },
                        },
                        {
                            "lifecycle": "RETIRED_FOR_RESTART",
                            "wrapper_pid": 9000,
                            "child_pid": 9001,
                            "tmux_session": "cc-retired",
                            "applied": {
                                "generation_id": "gen-1",
                                "revision": 1,
                                "spec_digest": f"sha256:{'3' * 64}",
                            },
                        },
                    ],
                }
            ],
        }
    }

    def factory(_path: Path, _timeout: float) -> RuntimeConnector:
        return cast(RuntimeConnector, _FakeConnector(response))

    result = asyncio.run(
        collect_runtime_binding_health(
            store_path=store_path,
            timeout=1.0,
            connector_factory=factory,
        )
    )
    item = result.bindings[0]
    assert item.health == "PENDING_CHANGE"
    assert item.desired.revision == 3
    assert item.desired.provider == "codex"
    assert item.applied[0].revision == 2
    assert item.active_instance_count == 1
    assert item.activity == "BUSY"
    assert result.telegram_e2e_verified is False
    encoded = result.model_dump_json()
    assert "wrapper_pid" not in encoded
    assert "child_pid" not in encoded
    assert "cc-private" not in encoded
    assert "cc-retired" not in encoded
    assert "must-not-be-public" not in encoded


def test_runtime_outage_is_visible_without_default_or_false_health(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    upsert_runtime_binding(store_path, _binding())

    def factory(_path: Path, _timeout: float) -> RuntimeConnector:
        return cast(
            RuntimeConnector,
            _FakeConnector(
                None,
                RuntimeConnectorError(
                    "RUNTIME_UNAVAILABLE",
                    "secret transport detail",
                    retryable=True,
                ),
            ),
        )

    result = asyncio.run(
        collect_runtime_binding_health(
            store_path=store_path,
            timeout=1.0,
            connector_factory=factory,
        )
    )
    assert result.bindings[0].health == "RUNTIME_UNAVAILABLE"
    assert result.bindings[0].runtime_available is False
    assert result.bindings[0].diagnostic_code == "RUNTIME_UNAVAILABLE"
    assert result.bindings[0].activity == "STOPPED"
    assert result.agent_instances[0].available is False
    assert "secret transport detail" not in result.model_dump_json()


def test_runtime_health_route_is_authenticated_and_returns_sanitized_dto(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    upsert_runtime_binding(store_path, _binding())
    monkeypatch.setenv("RUNTIME_CONTROL_PATH", str(store_path))
    monkeypatch.setenv("CUS_PREVIEW_TOKEN", "preview-secret")
    get_settings.cache_clear()

    class _RouteConnector(_FakeConnector):
        def __init__(self, _path: Path, *, timeout: float) -> None:
            assert timeout > 0
            super().__init__(
                {
                    "status": {
                        "agent_instance_id": "agent-1",
                        "bindings": [],
                    }
                }
            )

    monkeypatch.setattr(
        "ai_account_manager.integrations.runtime_health.RuntimeConnector",
        _RouteConnector,
    )
    try:
        client = TestClient(create_app())
        denied = client.get("/api/runtime/bindings/health")
        assert denied.status_code == 401
        response = client.get(
            "/api/runtime/bindings/health",
            headers={"X-CUS-Preview-Token": "preview-secret"},
        )
        assert response.status_code == 200
        assert response.json()["bindings"][0]["health"] == "OUT_OF_SYNC"
        assert response.json()["bindings"][0]["activity"] == "STOPPED"
        assert response.json()["telegram_e2e_verified"] is False
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("lifecycles", "expected"),
    [
        (["IDLE"], "IDLE"),
        (["IDLE", "BUSY"], "BUSY"),
        (["PROVISIONAL_STARTING"], "UNKNOWN"),
        (["EXITED", "RETIRED_FOR_RESTART"], "STOPPED"),
    ],
)
def test_runtime_activity_is_aggregated_without_process_details(
    tmp_path: Path,
    lifecycles: list[str],
    expected: str,
) -> None:
    store_path = tmp_path / "runtime-control.json"
    binding = _binding()
    register_agent_instance(store_path, _agent())
    upsert_runtime_binding(store_path, binding)
    instances = [
        {
            "lifecycle": lifecycle,
            "tmux_session": f"private-{index}",
            "wrapper_pid": 1000 + index,
        }
        for index, lifecycle in enumerate(lifecycles)
    ]
    response = {
        "status": {
            "agent_instance_id": "agent-1",
            "bindings": [
                {
                    "binding_id": "binding-1",
                    "source_ref": {"agent_instance_id": "agent-1"},
                    "health": "RUNTIME_HEALTHY",
                    "instances": instances,
                }
            ],
        }
    }

    def factory(_path: Path, _timeout: float) -> RuntimeConnector:
        return cast(RuntimeConnector, _FakeConnector(response))

    result = asyncio.run(
        collect_runtime_binding_health(
            store_path=store_path,
            timeout=1.0,
            connector_factory=factory,
        )
    )
    assert result.bindings[0].activity == expected
    encoded = result.model_dump_json()
    assert "private-" not in encoded
    assert "wrapper_pid" not in encoded
