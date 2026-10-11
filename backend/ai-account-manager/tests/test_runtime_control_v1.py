from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    acknowledge_project_relocation,
    apply_runtime_snapshot,
    commit_binding_selection,
    prepare_project_relocation,
    read_runtime_control_store,
    register_agent_instance,
    upsert_runtime_binding,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    ProjectRelocationRecord,
    RuntimeBindingRecord,
    canonical_digest,
)

_HEADER = struct.Struct("!I")


def _agent(
    agent_instance_id: str = "agent-1",
    *,
    chat_id: int = -1001,
    fingerprint_digit: str = "1",
) -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id=agent_instance_id,
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity=f"bot-{agent_instance_id}",
        telegram_bot_token_fingerprint="sha256:" + fingerprint_digit * 64,
        forum_chat_id=chat_id,
        runtime_socket=f"/run/user/1000/ai-agent-runtime/{agent_instance_id}/runtime.sock",
    )


def _binding(
    *,
    binding_id: str = "binding-1",
    thread_id: int = 42,
    project_id: str = "project-1",
    cwd: str = "/srv/project",
    revision: int = 1,
    generation: str = "gen-1",
    model: str = "gpt-test",
    lifecycle_status: str = "CONFIGURED",
) -> RuntimeBindingRecord:
    digest_payload: dict[str, object] = {
        "binding_id": binding_id,
        "agent_instance_id": "agent-1",
        "generation_id": generation,
        "revision": revision,
        "project_id": project_id,
        "canonical_cwd": cwd,
        "provider": "codex",
        "account_profile": "work",
        "model": model,
        "reasoning_effort": "high",
    }
    desired = DesiredSpecRecord(
        **digest_payload,
        spec_digest=canonical_digest(digest_payload),
        source_kind="cus_ui",
        source_ref="user:1",
    )
    return RuntimeBindingRecord(
        binding_id=binding_id,
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=thread_id,
        topic_name="Project",
        project_id=project_id,
        canonical_cwd=cwd,
        desired=desired,
        lifecycle_status=lifecycle_status,
    )


def test_desired_digest_and_binding_identity_are_validated() -> None:
    binding = _binding()
    assert binding.desired.spec_digest.startswith("sha256:")
    raw = binding.desired.model_dump()
    raw["model"] = "tampered"
    with pytest.raises(ValidationError, match="spec_digest"):
        DesiredSpecRecord.model_validate(raw)


def test_digest_uses_runtime_compatible_utf8_canonical_json() -> None:
    payload: dict[str, object] = {"canonical_cwd": "/srv/проект", "model": "модель"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert canonical_digest(payload) == f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def test_store_enforces_agent_uniqueness_and_revision_contract(tmp_path: Path) -> None:
    path = tmp_path / "runtime_control.json"
    register_agent_instance(path, _agent())
    with pytest.raises(RuntimeControlStoreError, match="Forum chat"):
        register_agent_instance(path, _agent("agent-2", fingerprint_digit="2"))
    with pytest.raises(RuntimeControlStoreError, match="fingerprint"):
        register_agent_instance(path, _agent("agent-2", chat_id=-1002))

    upsert_runtime_binding(path, _binding(revision=2))
    with pytest.raises(RuntimeControlStoreError) as stale:
        upsert_runtime_binding(path, _binding(revision=1))
    assert stale.value.code == "desired_revision_stale"
    with pytest.raises(RuntimeControlStoreError) as generation:
        upsert_runtime_binding(path, _binding(generation="gen-2"))
    assert generation.value.code == "generation_snapshot_required"
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-2",
        records=(_binding(generation="gen-2"),),
    )
    stored = read_runtime_control_store(path)
    assert stored.bindings["binding-1"].desired.generation_id == "gen-2"
    assert path.stat().st_mode & 0o777 == 0o600


def test_manual_selection_updates_only_its_topic_with_cas(tmp_path: Path) -> None:
    path = tmp_path / "runtime_control.json"
    register_agent_instance(path, _agent())
    first = _binding(binding_id="first", thread_id=42)
    second = _binding(binding_id="second", thread_id=43)
    upsert_runtime_binding(path, first)
    upsert_runtime_binding(path, second)

    result, changed = commit_binding_selection(
        path,
        binding_id="first",
        expected_generation_id=first.desired.generation_id,
        expected_revision=first.desired.revision,
        expected_spec_digest=first.desired.spec_digest,
        account_profile="backup",
        model="gpt-test-2",
        reasoning_effort="medium",
        source_ref="telegram-user:777001",
    )
    assert result == "UPDATED"
    assert changed.desired.revision == 2
    assert changed.desired.account_profile == "backup"
    assert changed.desired.source_kind == "cus_ui"
    assert read_runtime_control_store(path).bindings["second"].desired == second.desired

    with pytest.raises(RuntimeControlStoreError) as stale:
        commit_binding_selection(
            path,
            binding_id="first",
            expected_generation_id=first.desired.generation_id,
            expected_revision=first.desired.revision,
            expected_spec_digest=first.desired.spec_digest,
            account_profile="work",
            model="gpt-test",
            reasoning_effort="high",
            source_ref="telegram-user:777001",
        )
    assert stale.value.code == "desired_revision_conflict"
    assert read_runtime_control_store(path).bindings["first"].desired == changed.desired

    no_change, same = commit_binding_selection(
        path,
        binding_id="first",
        expected_generation_id=changed.desired.generation_id,
        expected_revision=changed.desired.revision,
        expected_spec_digest=changed.desired.spec_digest,
        account_profile="backup",
        model="gpt-test-2",
        reasoning_effort="medium",
        source_ref="telegram-user:777001",
    )
    assert no_change == "UNCHANGED"
    assert same.desired == changed.desired


def test_agent_instance_identity_is_immutable_and_release_change_requires_inactive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime_control.json"
    original = _agent()
    register_agent_instance(path, original)

    with pytest.raises(RuntimeControlStoreError) as identity:
        register_agent_instance(
            path,
            original.model_copy(update={"telegram_bot_identity": "another-working-bot"}),
        )
    assert identity.value.code == "agent_instance_identity_changed"

    changed_release = original.model_copy(
        update={
            "pinned_upstream_sha": "a" * 40,
            "runtime_socket": "/run/user/1000/ai-agent-runtime/agent-1-v2/runtime.sock",
        }
    )
    with pytest.raises(RuntimeControlStoreError) as active_change:
        register_agent_instance(path, changed_release)
    assert active_change.value.code == "agent_instance_must_be_inactive"

    register_agent_instance(path, original.model_copy(update={"active": False}))
    inactive_release = changed_release.model_copy(update={"active": False})
    register_agent_instance(path, inactive_release)
    register_agent_instance(path, inactive_release.model_copy(update={"active": True}))

    stored = read_runtime_control_store(path).agent_instances[original.agent_instance_id]
    assert stored.active is True
    assert stored.pinned_upstream_sha == "a" * 40
    assert stored.runtime_socket.endswith("agent-1-v2/runtime.sock")


def test_snapshot_requires_complete_inventory_and_explicit_archive(tmp_path: Path) -> None:
    path = tmp_path / "runtime_control.json"
    register_agent_instance(path, _agent())
    first = _binding()
    second = _binding(
        binding_id="binding-2",
        thread_id=43,
        project_id="project-2",
        cwd="/srv/project-2",
    )
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-1",
        records=(first, second),
    )
    with pytest.raises(RuntimeControlStoreError) as incomplete:
        apply_runtime_snapshot(
            path,
            agent_instance_id="agent-1",
            generation_id="gen-2",
            records=(_binding(generation="gen-2"),),
        )
    assert incomplete.value.code == "runtime_snapshot_incomplete"

    archived = second.model_copy(update={"lifecycle_status": "ARCHIVED"})
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-2",
        records=(_binding(generation="gen-2"), archived),
    )
    stored = read_runtime_control_store(path)
    assert stored.bindings["binding-2"].lifecycle_status == "ARCHIVED"


def test_project_relocation_is_explicit_complete_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runtime_control.json"
    register_agent_instance(path, _agent())
    first = _binding()
    second = _binding(binding_id="binding-2", thread_id=43)
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-1",
        records=(first, second),
    )

    relocated_store, relocation = prepare_project_relocation(
        path,
        project_id="project-1",
        old_canonical_cwd="/srv/project",
        new_canonical_cwd="/srv/project-moved",
        relocation_id="rel_" + "1" * 32,
    )

    assert relocation.binding_ids == ("binding-1", "binding-2")
    assert relocation.pending_agent_instance_ids == ("agent-1",)
    assert relocation.complete is False
    for binding_id in relocation.binding_ids:
        binding = relocated_store.bindings[binding_id]
        assert binding.project_id == "project-1"
        assert binding.canonical_cwd == "/srv/project-moved"
        assert binding.desired.canonical_cwd == "/srv/project-moved"
        assert binding.desired.revision == 2
        assert binding.desired.source_kind == "project_relocation"
        assert binding.desired.source_ref == relocation.relocation_id

    replay_store, replay = prepare_project_relocation(
        path,
        project_id="project-1",
        old_canonical_cwd="/srv/project",
        new_canonical_cwd="/srv/project-moved",
        relocation_id=relocation.relocation_id,
    )
    assert replay == relocation
    assert replay_store.store_revision == relocated_store.store_revision

    acknowledged = acknowledge_project_relocation(
        path,
        relocation_id=relocation.relocation_id,
        agent_instance_id="agent-1",
    )
    assert acknowledged.complete is True
    acknowledged_again = acknowledge_project_relocation(
        path,
        relocation_id=relocation.relocation_id,
        agent_instance_id="agent-1",
    )
    assert acknowledged_again == acknowledged


def test_project_relocation_rejects_unmanaged_or_colliding_project(tmp_path: Path) -> None:
    path = tmp_path / "runtime_control.json"
    register_agent_instance(path, _agent())
    unmanaged = _binding().model_copy(update={"lifecycle_status": "UNMANAGED"})
    other = _binding(
        binding_id="binding-2",
        thread_id=43,
        project_id="project-2",
        cwd="/srv/other",
    )
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-1",
        records=(unmanaged, other),
    )
    with pytest.raises(RuntimeControlStoreError) as not_managed:
        prepare_project_relocation(
            path,
            project_id="project-1",
            old_canonical_cwd="/srv/project",
            new_canonical_cwd="/srv/new",
        )
    assert not_managed.value.code == "project_relocation_not_managed"

    configured = unmanaged.model_copy(update={"lifecycle_status": "CONFIGURED"})
    apply_runtime_snapshot(
        path,
        agent_instance_id="agent-1",
        generation_id="gen-1",
        records=(configured, other),
    )
    with pytest.raises(RuntimeControlStoreError) as collision:
        prepare_project_relocation(
            path,
            project_id="project-1",
            old_canonical_cwd="/srv/project",
            new_canonical_cwd="/srv/other",
        )
    assert collision.value.code == "project_relocation_collision"


def test_connector_uses_versioned_length_delimited_unix_protocol(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "runtime.sock"
        received: list[dict[str, Any]] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readexactly(_HEADER.size)
            (size,) = _HEADER.unpack(header)
            payload = json.loads(await reader.readexactly(size))
            received.append(payload)
            response = {
                "protocol_major": 1,
                "protocol_minor": 0,
                "action": "response",
                "request_id": payload["request_id"],
                "ok": True,
                "binding_id": "binding-1",
            }
            encoded = json.dumps(response).encode()
            writer.write(_HEADER.pack(len(encoded)) + encoded)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        socket_path.chmod(0o600)
        server_task = asyncio.create_task(server.serve_forever())
        connector = RuntimeConnector(socket_path)
        result = await connector.apply_binding(_binding(), request_id="request-1")
        assert result["binding_id"] == "binding-1"
        assert received[0]["action"] == "apply_binding"
        assert received[0]["desired"]["spec_digest"] == _binding().desired.spec_digest
        assert received[0]["desired"]["source_kind"] == "cus_ui"
        assert received[0]["desired"]["source_ref"] == "user:1"
        assert received[0]["binding"]["thread_id"] == 42

        snapshot = await connector.apply_snapshot(
            agent_instance_id="agent-1",
            generation_id="gen-1",
            bindings=(_binding(),),
            project_relocations=(
                ProjectRelocationRecord(
                    relocation_id="rel_" + "2" * 32,
                    project_id="project-1",
                    old_canonical_cwd="/srv/project-old",
                    new_canonical_cwd="/srv/project",
                    binding_ids=("binding-1",),
                    pending_agent_instance_ids=("agent-1",),
                    created_at=datetime.now(timezone.utc),
                ),
            ),
            request_id="snapshot-1",
        )
        assert snapshot["binding_id"] == "binding-1"
        assert received[1]["action"] == "apply_snapshot"
        assert received[1]["bindings"][0]["binding"]["thread_id"] == 42
        assert received[1]["bindings"][0]["desired"]["source_kind"] == "cus_ui"
        assert received[1]["bindings"][0]["desired"]["source_ref"] == "user:1"
        assert received[1]["unmanaged_bindings"] == []
        assert received[1]["project_relocations"] == [
            {
                "relocation_id": "rel_" + "2" * 32,
                "agent_instance_id": "agent-1",
                "project_id": "project-1",
                "old_canonical_cwd": "/srv/project-old",
                "new_canonical_cwd": "/srv/project",
                "binding_ids": ["binding-1"],
            }
        ]
        assert received[1]["snapshot_digest"].startswith("sha256:")

        heartbeat = await connector.control_plane_heartbeat("agent-1")
        assert heartbeat["binding_id"] == "binding-1"
        assert received[2]["action"] == "control_plane_heartbeat"
        assert received[2]["agent_instance_id"] == "agent-1"

        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_connector_keeps_unmanaged_binding_out_of_managed_apply_list(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "runtime.sock"
        received: list[dict[str, Any]] = []

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readexactly(_HEADER.size)
            (size,) = _HEADER.unpack(header)
            payload = json.loads(await reader.readexactly(size))
            received.append(payload)
            response = {
                "protocol_major": 1,
                "protocol_minor": 0,
                "action": "response",
                "request_id": payload["request_id"],
                "ok": True,
            }
            encoded = json.dumps(response).encode()
            writer.write(_HEADER.pack(len(encoded)) + encoded)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        socket_path.chmod(0o600)
        server_task = asyncio.create_task(server.serve_forever())
        connector = RuntimeConnector(socket_path)
        unmanaged = _binding().model_copy(update={"lifecycle_status": "UNMANAGED"})
        await connector.apply_snapshot(
            agent_instance_id="agent-1",
            generation_id="gen-1",
            bindings=(unmanaged,),
            request_id="snapshot-unmanaged",
        )
        assert received[0]["bindings"] == []
        assert received[0]["unmanaged_bindings"][0]["binding"]["binding_id"] == "binding-1"
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_connector_reports_runtime_outage_without_fallback(tmp_path: Path) -> None:
    async def scenario() -> None:
        connector = RuntimeConnector(tmp_path / "missing.sock", timeout=0.1)
        with pytest.raises(RuntimeConnectorError) as failure:
            await connector.apply_binding(_binding())
        assert failure.value.code == "RUNTIME_UNAVAILABLE"
        assert failure.value.retryable is True

    asyncio.run(scenario())


def test_connector_rejects_non_socket_or_permissive_runtime_endpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        socket_path = tmp_path / "runtime.sock"
        socket_path.write_text("not a socket", encoding="utf-8")
        connector = RuntimeConnector(socket_path)
        with pytest.raises(RuntimeConnectorError) as regular_failure:
            await connector.status()
        assert regular_failure.value.code == "UNSAFE_RUNTIME_SOCKET"
        assert regular_failure.value.retryable is False

        socket_path.unlink()

        async def handler(
            _reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        socket_path.chmod(0o666)
        with pytest.raises(RuntimeConnectorError) as mode_failure:
            await connector.status()
        assert mode_failure.value.code == "UNSAFE_RUNTIME_SOCKET"
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
