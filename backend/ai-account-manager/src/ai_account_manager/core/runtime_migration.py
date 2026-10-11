"""Versioned, fail-closed import of Runtime Binding migration plans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    apply_runtime_snapshot,
    read_runtime_control_store,
)
from ai_account_manager.schemas.runtime_control import (
    DesiredSpecRecord,
    RuntimeBindingRecord,
    RuntimeControlStore,
    canonical_digest,
)

PLAN_SCHEMA = "ai-agent-runtime.binding-migration-plan.v1"
_MAX_PLAN_BYTES = 16 * 1024 * 1024


class RuntimeMigrationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeMigration:
    plan_digest: str
    source_inventory_digest: str
    agent_instance_id: str
    generation_id: str
    records: tuple[RuntimeBindingRecord, ...]
    excluded_binding_ids: tuple[str, ...]

    @property
    def managed_count(self) -> int:
        return sum(record.lifecycle_status == "CONFIGURED" for record in self.records)

    @property
    def unmanaged_count(self) -> int:
        return sum(record.lifecycle_status == "UNMANAGED" for record in self.records)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded_binding_ids)


def _read_plan(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded.is_symlink():
        raise RuntimeMigrationError(
            "migration_plan_path_invalid",
            "Migration plan path must be absolute, traversal-free, and not a symlink",
        )
    if not expanded.is_file() or expanded.stat().st_size > _MAX_PLAN_BYTES:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration plan is not a bounded regular file",
        )
    try:
        payload = json.loads(expanded.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration plan is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration plan must contain a JSON object",
        )
    return payload


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            f"Migration plan {field} must be a non-empty string",
        )
    return value


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            f"Migration plan {field} must be an integer",
        )
    return value


def _verify_plan(payload: dict[str, Any]) -> tuple[str, str, str]:
    if payload.get("schema") != PLAN_SCHEMA:
        raise RuntimeMigrationError(
            "migration_plan_schema_unsupported",
            "Migration plan schema is unsupported",
        )
    plan_digest = _required_string(payload.get("plan_digest"), "plan_digest")
    unsigned = dict(payload)
    unsigned.pop("plan_digest", None)
    if canonical_digest(unsigned) != plan_digest:
        raise RuntimeMigrationError(
            "migration_plan_digest_mismatch",
            "Migration plan digest verification failed",
        )
    if payload.get("accepted") is not True:
        raise RuntimeMigrationError(
            "migration_plan_blocked",
            "Migration plan contains blocking findings",
        )
    source_digest = _required_string(
        payload.get("source_inventory_digest"),
        "source_inventory_digest",
    )
    generation_id = _required_string(payload.get("generation_id"), "generation_id")
    return plan_digest, source_digest, generation_id


def _record_from_entry(
    entry: dict[str, Any],
    *,
    generation_id: str,
    source_decisions_digest: str | None,
) -> RuntimeBindingRecord | None:
    status = entry.get("status")
    if status == "BLOCKED":
        raise RuntimeMigrationError(
            "migration_plan_blocked",
            "Accepted migration plan contains a blocked entry",
        )
    if status == "EXCLUDED":
        source_ref = _required_string(entry.get("source_ref"), "entry.source_ref")
        _required_string(entry.get("binding_id"), "entry.binding_id")
        identity = entry.get("identity")
        exclusion = entry.get("exclusion")
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("thread_id"), int)
            or not isinstance(identity.get("topic_name"), str)
            or not isinstance(exclusion, dict)
            or exclusion.get("action") != "EXCLUDE_DELETED_TOPIC"
            or exclusion.get("approved_by_role") != "owner"
            or exclusion.get("decisions_digest") != source_decisions_digest
            or entry.get("desired") is not None
            or entry.get("blockers") != []
        ):
            raise RuntimeMigrationError(
                "migration_plan_exclusion_invalid",
                "Excluded migration entry lacks exact owner-decision evidence",
            )
        _required_string(exclusion.get("reason"), "entry.exclusion.reason")
        _required_string(exclusion.get("approved_at"), "entry.exclusion.approved_at")
        assert source_ref
        return None
    if status not in {"READY", "UNMANAGED"}:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration entry status is invalid",
        )
    identity = entry.get("identity")
    desired_payload = entry.get("desired")
    if not isinstance(identity, dict) or not isinstance(desired_payload, dict):
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Managed and unmanaged migration entries require identity and desired",
        )
    source_ref = _required_string(entry.get("source_ref"), "entry.source_ref")
    binding_id = _required_string(entry.get("binding_id"), "entry.binding_id")
    project_id = _required_string(entry.get("project_id"), "entry.project_id")
    topic_name = _required_string(identity.get("topic_name"), "entry.identity.topic_name")
    agent_instance_id = _required_string(
        identity.get("agent_instance_id"),
        "entry.identity.agent_instance_id",
    )
    chat_id = _required_int(identity.get("chat_id"), "entry.identity.chat_id")
    thread_id = _required_int(identity.get("thread_id"), "entry.identity.thread_id")
    canonical_cwd = _required_string(
        identity.get("canonical_cwd"),
        "entry.identity.canonical_cwd",
    )
    try:
        desired = DesiredSpecRecord.model_validate(desired_payload)
    except ValidationError as exc:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration desired spec is invalid",
        ) from exc
    if desired.generation_id != generation_id:
        raise RuntimeMigrationError(
            "migration_plan_generation_mismatch",
            "Migration desired generation differs from plan generation",
        )
    if desired.source_kind != "migration" or desired.source_ref != source_ref:
        raise RuntimeMigrationError(
            "migration_plan_provenance_invalid",
            "Migration desired provenance is invalid",
        )
    lifecycle: Literal["CONFIGURED", "UNMANAGED"] = (
        "CONFIGURED" if status == "READY" else "UNMANAGED"
    )
    try:
        return RuntimeBindingRecord(
            binding_id=binding_id,
            agent_instance_id=agent_instance_id,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_name=topic_name,
            project_id=project_id,
            canonical_cwd=canonical_cwd,
            desired=desired,
            lifecycle_status=lifecycle,
        )
    except ValidationError as exc:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration binding identity is invalid",
        ) from exc


def prepare_runtime_migration(
    *,
    plan_path: Path,
    store_path: Path,
) -> PreparedRuntimeMigration:
    payload = _read_plan(plan_path)
    plan_digest, source_digest, generation_id = _verify_plan(payload)
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration plan must contain entries",
        )
    records: list[RuntimeBindingRecord] = []
    excluded_binding_ids: list[str] = []
    excluded_identities: list[tuple[str, int, int, str]] = []
    source_decisions_digest = payload.get("source_decisions_digest")
    if source_decisions_digest is not None and not isinstance(source_decisions_digest, str):
        raise RuntimeMigrationError(
            "migration_plan_invalid",
            "Migration plan source_decisions_digest is invalid",
        )
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise RuntimeMigrationError(
                "migration_plan_invalid",
                "Migration plan contains a malformed entry",
            )
        record = _record_from_entry(
            raw_entry,
            generation_id=generation_id,
            source_decisions_digest=source_decisions_digest,
        )
        if record is not None:
            records.append(record)
        elif raw_entry.get("status") == "EXCLUDED":
            binding_id = _required_string(raw_entry.get("binding_id"), "entry.binding_id")
            identity = raw_entry.get("identity")
            assert isinstance(identity, dict)
            source_ref = _required_string(raw_entry.get("source_ref"), "entry.source_ref")
            agent_instance_id = _required_string(
                identity.get("agent_instance_id"),
                "entry.identity.agent_instance_id",
            )
            chat_id = _required_int(identity.get("chat_id"), "entry.identity.chat_id")
            thread_id = _required_int(identity.get("thread_id"), "entry.identity.thread_id")
            if source_ref != f"{agent_instance_id}/{chat_id}/{thread_id}":
                raise RuntimeMigrationError(
                    "migration_plan_exclusion_invalid",
                    "Excluded migration source_ref does not match its exact identity",
                )
            excluded_binding_ids.append(binding_id)
            excluded_identities.append(
                (agent_instance_id, chat_id, thread_id, source_ref)
            )
    if excluded_binding_ids:
        _required_string(source_decisions_digest, "source_decisions_digest")
        if len(set(excluded_binding_ids)) != len(excluded_binding_ids):
            raise RuntimeMigrationError(
                "migration_plan_exclusion_invalid",
                "Migration plan contains duplicate excluded binding IDs",
            )
        if len({item[3] for item in excluded_identities}) != len(excluded_identities):
            raise RuntimeMigrationError(
                "migration_plan_exclusion_invalid",
                "Migration plan contains duplicate excluded source refs",
            )
    if not records:
        raise RuntimeMigrationError(
            "migration_plan_empty",
            "Migration plan contains no Runtime Bindings",
        )
    agent_ids = {record.agent_instance_id for record in records}
    if len(agent_ids) != 1:
        raise RuntimeMigrationError(
            "migration_plan_namespace_mismatch",
            "Migration plan must target exactly one AgentInstance",
        )
    agent_instance_id = next(iter(agent_ids))
    if len({record.binding_id for record in records}) != len(records):
        raise RuntimeMigrationError(
            "migration_plan_duplicate_binding",
            "Migration plan contains duplicate binding IDs",
        )
    if len({(record.chat_id, record.thread_id) for record in records}) != len(records):
        raise RuntimeMigrationError(
            "migration_plan_duplicate_topic",
            "Migration plan contains duplicate topic identities",
        )
    try:
        store = read_runtime_control_store(store_path)
    except RuntimeControlStoreError as exc:
        raise RuntimeMigrationError(exc.code, exc.message) from exc
    agent = store.agent_instances.get(agent_instance_id)
    if agent is None or not agent.active:
        raise RuntimeMigrationError(
            "migration_agent_instance_unavailable",
            "Migration plan has no active registered AgentInstance",
        )
    if any(record.chat_id != agent.forum_chat_id for record in records):
        raise RuntimeMigrationError(
            "migration_plan_forum_mismatch",
            "Migration binding forum differs from AgentInstance",
        )
    if any(
        excluded_agent_id != agent_instance_id or excluded_chat_id != agent.forum_chat_id
        for excluded_agent_id, excluded_chat_id, _thread_id, _source_ref in excluded_identities
    ):
        raise RuntimeMigrationError(
            "migration_plan_exclusion_invalid",
            "Excluded migration entry differs from the plan AgentInstance/forum",
        )
    active_topics = {(record.chat_id, record.thread_id) for record in records}
    excluded_topics = {(chat_id, thread_id) for _, chat_id, thread_id, _ in excluded_identities}
    if active_topics.intersection(excluded_topics):
        raise RuntimeMigrationError(
            "migration_plan_exclusion_invalid",
            "A migration topic cannot be both active and excluded",
        )
    known = {
        binding_id
        for binding_id, binding in store.bindings.items()
        if binding.agent_instance_id == agent_instance_id
    }
    incoming = {record.binding_id for record in records}
    if known.intersection(excluded_binding_ids):
        raise RuntimeMigrationError(
            "migration_plan_excludes_existing_binding",
            "Migration exclusion cannot implicitly delete an existing CUS Runtime Binding",
        )
    if known - incoming:
        raise RuntimeMigrationError(
            "migration_plan_incomplete",
            "Migration plan omits existing CUS Runtime Bindings",
        )
    return PreparedRuntimeMigration(
        plan_digest=plan_digest,
        source_inventory_digest=source_digest,
        agent_instance_id=agent_instance_id,
        generation_id=generation_id,
        records=tuple(sorted(records, key=lambda record: record.binding_id)),
        excluded_binding_ids=tuple(sorted(excluded_binding_ids)),
    )


def commit_runtime_migration(
    store_path: Path,
    prepared: PreparedRuntimeMigration,
) -> RuntimeControlStore:
    try:
        return apply_runtime_snapshot(
            store_path,
            agent_instance_id=prepared.agent_instance_id,
            generation_id=prepared.generation_id,
            records=prepared.records,
        )
    except RuntimeControlStoreError as exc:
        raise RuntimeMigrationError(exc.code, exc.message) from exc
