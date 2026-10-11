from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_account_manager.core.runtime_control import (
    read_runtime_control_store,
    register_agent_instance,
)
from ai_account_manager.core.runtime_migration import (
    RuntimeMigrationError,
    commit_runtime_migration,
    prepare_runtime_migration,
)
from ai_account_manager.schemas.runtime_control import AgentInstanceRecord, canonical_digest


def _agent() -> AgentInstanceRecord:
    return AgentInstanceRecord(
        agent_instance_id="agent-1",
        pinned_upstream_sha="0" * 40,
        telegram_bot_identity="runtime-bot",
        telegram_bot_token_fingerprint="sha256:" + "1" * 64,
        forum_chat_id=-1001,
        runtime_socket="/run/user/1000/runtime.sock",
    )


def _entry(thread_id: int, *, status: str) -> dict[str, object]:
    binding_id = f"binding-{thread_id}"
    project_id = "project-shared"
    cwd = "/srv/repos/shared"
    source_ref = f"agent-1/-1001/{thread_id}"
    desired: dict[str, object] = {
        "binding_id": binding_id,
        "agent_instance_id": "agent-1",
        "generation_id": "migration-1",
        "revision": 1,
        "project_id": project_id,
        "canonical_cwd": cwd,
        "provider": "codex",
        "account_profile": f"account-{thread_id}",
        "model": "gpt-test",
        "reasoning_effort": "high",
    }
    desired["spec_digest"] = canonical_digest(desired)
    desired["source_kind"] = "migration"
    desired["source_ref"] = source_ref
    return {
        "source_ref": source_ref,
        "status": status,
        "binding_id": binding_id,
        "project_id": project_id,
        "identity": {
            "agent_instance_id": "agent-1",
            "chat_id": -1001,
            "thread_id": thread_id,
            "topic_name": f"Topic {thread_id}",
            "canonical_cwd": cwd,
        },
        "desired": desired,
        "blockers": [],
    }


def _write_plan(path: Path, entries: list[dict[str, object]]) -> dict[str, object]:
    decisions_digest = "sha256:" + "3" * 64 if any(
        entry["status"] == "EXCLUDED" for entry in entries
    ) else None
    payload: dict[str, object] = {
        "schema": "ai-agent-runtime.binding-migration-plan.v1",
        "source_inventory_digest": "sha256:" + "2" * 64,
        "generation_id": "migration-1",
        "source_decisions_digest": decisions_digest,
        "accepted": True,
        "counts": {
            "READY": sum(entry["status"] == "READY" for entry in entries),
            "UNMANAGED": sum(entry["status"] == "UNMANAGED" for entry in entries),
            "EXCLUDED": sum(entry["status"] == "EXCLUDED" for entry in entries),
            "BLOCKED": 0,
        },
        "duplicate_binding_ids": {},
        "conflicting_projects": {},
        "entries": entries,
    }
    payload["plan_digest"] = canonical_digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _excluded_entry(thread_id: int) -> dict[str, object]:
    source_ref = f"agent-1/-1001/{thread_id}"
    return {
        "source_ref": source_ref,
        "status": "EXCLUDED",
        "binding_id": f"binding-{thread_id}",
        "project_id": None,
        "identity": {
            "agent_instance_id": "agent-1",
            "chat_id": -1001,
            "thread_id": thread_id,
            "topic_name": f"Topic {thread_id}",
            "canonical_cwd": None,
        },
        "desired": None,
        "blockers": [],
        "exclusion": {
            "action": "EXCLUDE_DELETED_TOPIC",
            "reason": "Topic no longer exists in the Telegram forum",
            "approved_by_role": "owner",
            "approved_at": "2026-10-04T12:00:00+03:00",
            "decisions_digest": "sha256:" + "3" * 64,
        },
    }


def test_imports_managed_and_unmanaged_bindings_atomically(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    plan_path = tmp_path / "binding-plan.json"
    _write_plan(plan_path, [_entry(42, status="READY"), _entry(43, status="UNMANAGED")])
    prepared = prepare_runtime_migration(plan_path=plan_path, store_path=store_path)
    assert prepared.managed_count == 1
    assert prepared.unmanaged_count == 1
    assert read_runtime_control_store(store_path).bindings == {}
    committed = commit_runtime_migration(store_path, prepared)
    assert committed.bindings["binding-42"].lifecycle_status == "CONFIGURED"
    assert committed.bindings["binding-43"].lifecycle_status == "UNMANAGED"
    assert committed.bindings["binding-42"].canonical_cwd == (
        committed.bindings["binding-43"].canonical_cwd
    )
    assert committed.bindings["binding-42"].desired.account_profile == "account-42"
    assert committed.bindings["binding-43"].desired.account_profile == "account-43"


def test_import_skips_explicit_owner_approved_deleted_topic(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    plan_path = tmp_path / "binding-plan.json"
    _write_plan(
        plan_path,
        [_entry(42, status="READY"), _excluded_entry(99)],
    )

    prepared = prepare_runtime_migration(plan_path=plan_path, store_path=store_path)

    assert prepared.excluded_count == 1
    assert prepared.excluded_binding_ids == ("binding-99",)
    committed = commit_runtime_migration(store_path, prepared)
    assert set(committed.bindings) == {"binding-42"}


def test_import_rejects_exclusion_that_would_delete_existing_binding(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    initial_path = tmp_path / "initial.json"
    _write_plan(initial_path, [_entry(42, status="READY"), _entry(99, status="READY")])
    commit_runtime_migration(
        store_path,
        prepare_runtime_migration(plan_path=initial_path, store_path=store_path),
    )
    exclusion_path = tmp_path / "exclusion.json"
    _write_plan(exclusion_path, [_entry(42, status="READY"), _excluded_entry(99)])

    with pytest.raises(RuntimeMigrationError) as failure:
        prepare_runtime_migration(plan_path=exclusion_path, store_path=store_path)

    assert failure.value.code == "migration_plan_excludes_existing_binding"


def test_import_rejects_tampered_or_blocked_plan(tmp_path: Path) -> None:
    store_path = tmp_path / "runtime-control.json"
    register_agent_instance(store_path, _agent())
    plan_path = tmp_path / "binding-plan.json"
    payload = _write_plan(plan_path, [_entry(42, status="READY")])
    payload["entries"][0]["identity"]["thread_id"] = 99  # type: ignore[index]
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeMigrationError) as tampered:
        prepare_runtime_migration(plan_path=plan_path, store_path=store_path)
    assert tampered.value.code == "migration_plan_digest_mismatch"

    blocked = _write_plan(plan_path, [_entry(42, status="READY")])
    blocked["accepted"] = False
    blocked.pop("plan_digest")
    blocked["plan_digest"] = canonical_digest(blocked)
    plan_path.write_text(json.dumps(blocked), encoding="utf-8")
    with pytest.raises(RuntimeMigrationError) as failure:
        prepare_runtime_migration(plan_path=plan_path, store_path=store_path)
    assert failure.value.code == "migration_plan_blocked"
