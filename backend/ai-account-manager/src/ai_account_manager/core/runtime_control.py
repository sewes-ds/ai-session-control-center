from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Literal

from pydantic import ValidationError

from ai_account_manager.schemas.runtime_control import (
    AgentInstanceRecord,
    DesiredSpecRecord,
    ObservedSpecProposalRecord,
    ProjectRelocationRecord,
    RuntimeBindingRecord,
    RuntimeControlStore,
    RuntimeEventReceipt,
    RuntimeNotificationReceipt,
    RuntimeUserNotificationRecord,
    canonical_digest,
)


class RuntimeControlStoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _validate_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts or expanded == Path("/"):
        raise RuntimeControlStoreError(
            "invalid_runtime_control_path",
            "Runtime control store path must be absolute, non-root, and traversal-free",
        )
    if expanded.is_symlink() or expanded.parent.is_symlink():
        raise RuntimeControlStoreError(
            "invalid_runtime_control_path",
            "Runtime control store and parent must not be symlinks",
        )
    return expanded


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = path.with_suffix(path.suffix + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: Path) -> RuntimeControlStore:
    if not path.exists():
        return RuntimeControlStore(store_revision=1)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise RuntimeControlStoreError(
            "runtime_control_invalid", "Runtime control store is invalid"
        )
    try:
        return RuntimeControlStore.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise RuntimeControlStoreError(
            "runtime_control_invalid", "Runtime control store is invalid"
        ) from exc


def _write(path: Path, store: RuntimeControlStore) -> None:
    encoded = (json.dumps(store.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode()
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def read_runtime_control_store(path: Path) -> RuntimeControlStore:
    safe_path = _validate_path(path)
    with _locked(safe_path):
        return _read(safe_path)


def _validate_agent_instance_registration(
    store: RuntimeControlStore,
    record: AgentInstanceRecord,
) -> None:
    socket_path = Path(record.runtime_socket)
    if not socket_path.is_absolute() or ".." in socket_path.parts or socket_path == Path("/"):
        raise RuntimeControlStoreError(
            "invalid_runtime_socket", "Runtime socket must be an absolute non-root path"
        )
    existing = store.agent_instances.get(record.agent_instance_id)
    if existing is not None:
        immutable_changed = (
            existing.telegram_bot_identity != record.telegram_bot_identity
            or existing.telegram_bot_token_fingerprint != record.telegram_bot_token_fingerprint
            or existing.forum_chat_id != record.forum_chat_id
        )
        if immutable_changed:
            raise RuntimeControlStoreError(
                "agent_instance_identity_changed",
                "AgentInstance bot/forum identity is immutable; use explicit migration",
            )
        release_changed = (
            existing.pinned_upstream_sha != record.pinned_upstream_sha
            or existing.runtime_socket != record.runtime_socket
        )
        if release_changed and (existing.active or record.active):
            raise RuntimeControlStoreError(
                "agent_instance_must_be_inactive",
                "Pinned release/socket can change only while AgentInstance is inactive",
            )
    if record.active:
        for known in store.agent_instances.values():
            if not known.active or known.agent_instance_id == record.agent_instance_id:
                continue
            if known.forum_chat_id == record.forum_chat_id:
                raise RuntimeControlStoreError(
                    "duplicate_active_forum",
                    "Forum chat already belongs to another active AgentInstance",
                )
            if known.telegram_bot_token_fingerprint == record.telegram_bot_token_fingerprint:
                raise RuntimeControlStoreError(
                    "duplicate_active_bot",
                    "Telegram bot token fingerprint must be unique",
                )


def validate_agent_instance_registration(
    path: Path,
    record: AgentInstanceRecord,
) -> RuntimeControlStore:
    """Validate an operator descriptor under the store lock without mutating state."""

    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        _validate_agent_instance_registration(store, record)
        return store


def register_agent_instance(path: Path, record: AgentInstanceRecord) -> RuntimeControlStore:
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        _validate_agent_instance_registration(store, record)
        if store.agent_instances.get(record.agent_instance_id) == record:
            return store
        updated = store.model_copy(deep=True)
        updated.agent_instances[record.agent_instance_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated


def upsert_runtime_binding(
    path: Path,
    record: RuntimeBindingRecord,
) -> RuntimeControlStore:
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        agent = store.agent_instances.get(record.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != record.chat_id:
            raise RuntimeControlStoreError(
                "agent_instance_unavailable", "Binding has no unique active AgentInstance"
            )
        for known in store.bindings.values():
            if (
                known.agent_instance_id == record.agent_instance_id
                and known.chat_id == record.chat_id
                and known.thread_id == record.thread_id
                and known.binding_id != record.binding_id
            ):
                raise RuntimeControlStoreError(
                    "duplicate_topic_binding", "Telegram topic already has another Runtime Binding"
                )
        existing = store.bindings.get(record.binding_id)
        if existing is not None:
            if (
                existing.agent_instance_id != record.agent_instance_id
                or existing.chat_id != record.chat_id
                or existing.thread_id != record.thread_id
                or existing.project_id != record.project_id
            ):
                raise RuntimeControlStoreError(
                    "binding_identity_changed", "Runtime Binding identity is immutable"
                )
            if existing.canonical_cwd != record.canonical_cwd:
                raise RuntimeControlStoreError(
                    "project_relocation_required",
                    "Project path change requires explicit relocation",
                )
            previous = existing.desired
            current = record.desired
            if previous.generation_id != current.generation_id:
                raise RuntimeControlStoreError(
                    "generation_snapshot_required",
                    "New desired generation requires authoritative snapshot reconcile",
                )
            elif current.revision < previous.revision:
                raise RuntimeControlStoreError(
                    "desired_revision_stale", "Desired revision cannot move backwards"
                )
            elif (
                current.revision == previous.revision
                and current.spec_digest != previous.spec_digest
            ):
                raise RuntimeControlStoreError(
                    "desired_revision_conflict",
                    "Same desired revision has another digest",
                )
        updated = store.model_copy(deep=True)
        updated.bindings[record.binding_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated


def commit_binding_selection(
    path: Path,
    *,
    binding_id: str,
    expected_generation_id: str,
    expected_revision: int,
    expected_spec_digest: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    source_ref: str,
) -> tuple[Literal["UPDATED", "UNCHANGED"], RuntimeBindingRecord]:
    """Commit a user-selected profile for one topic without touching live work."""

    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        binding = store.bindings.get(binding_id)
        if binding is None or binding.lifecycle_status != "CONFIGURED":
            raise RuntimeControlStoreError(
                "runtime_binding_unavailable", "Configured Runtime Binding is unavailable"
            )
        agent = store.agent_instances.get(binding.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != binding.chat_id:
            raise RuntimeControlStoreError(
                "agent_instance_unavailable", "Binding AgentInstance is unavailable"
            )
        desired = binding.desired
        if (
            desired.generation_id != expected_generation_id
            or desired.revision != expected_revision
            or desired.spec_digest != expected_spec_digest
        ):
            raise RuntimeControlStoreError(
                "desired_revision_conflict", "Binding settings changed; refresh before saving"
            )
        if desired.provider != "codex":
            raise RuntimeControlStoreError(
                "provider_settings_not_supported", "Manual selection requires managed Codex"
            )
        if (
            desired.account_profile == account_profile
            and desired.model == model
            and desired.reasoning_effort == reasoning_effort
        ):
            return "UNCHANGED", binding
        digest_payload = {
            **desired.digest_payload(),
            "revision": desired.revision + 1,
            "account_profile": account_profile,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
        next_desired = DesiredSpecRecord(
            **digest_payload,
            spec_digest=canonical_digest(digest_payload),
            source_kind="cus_ui",
            source_ref=source_ref,
        )
        committed = binding.model_copy(update={"desired": next_desired})
        updated = store.model_copy(deep=True)
        updated.bindings[binding_id] = committed
        updated.store_revision += 1
        _write(safe_path, updated)
        return "UPDATED", committed


def _validate_project_path_text(value: str, *, field: str) -> str:
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise RuntimeControlStoreError(
            "project_relocation_invalid",
            f"Project relocation {field} must be absolute, non-root, and traversal-free",
        )
    return str(path)


def prepare_project_relocation(
    path: Path,
    *,
    project_id: str,
    old_canonical_cwd: str,
    new_canonical_cwd: str,
    relocation_id: str | None = None,
) -> tuple[RuntimeControlStore, ProjectRelocationRecord]:
    """Atomically record an explicit Project alias and advance every desired binding."""

    if not project_id:
        raise RuntimeControlStoreError(
            "project_relocation_invalid", "Project relocation requires project identity"
        )
    old_cwd = _validate_project_path_text(old_canonical_cwd, field="old path")
    new_cwd = _validate_project_path_text(new_canonical_cwd, field="new path")
    if old_cwd == new_cwd:
        raise RuntimeControlStoreError(
            "project_relocation_invalid", "Project relocation must change canonical cwd"
        )
    normalized_id = relocation_id or f"rel_{uuid.uuid4().hex}"
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        existing_relocation = store.project_relocations.get(normalized_id)
        if existing_relocation is not None:
            if (
                existing_relocation.project_id != project_id
                or existing_relocation.old_canonical_cwd != old_cwd
                or existing_relocation.new_canonical_cwd != new_cwd
            ):
                raise RuntimeControlStoreError(
                    "project_relocation_reused",
                    "Project relocation id was reused with different data",
                )
            return store, existing_relocation
        if any(
            record.project_id == project_id and not record.complete
            for record in store.project_relocations.values()
        ):
            raise RuntimeControlStoreError(
                "project_relocation_pending",
                "Project already has an incomplete relocation",
            )
        project_bindings = tuple(
            binding
            for binding in store.bindings.values()
            if binding.project_id == project_id and binding.lifecycle_status != "ARCHIVED"
        )
        if not project_bindings:
            raise RuntimeControlStoreError(
                "project_relocation_invalid", "Project has no active Runtime Bindings"
            )
        if any(binding.lifecycle_status != "CONFIGURED" for binding in project_bindings):
            raise RuntimeControlStoreError(
                "project_relocation_not_managed",
                "Project relocation requires every active binding to be managed",
            )
        if {binding.canonical_cwd for binding in project_bindings} != {old_cwd}:
            raise RuntimeControlStoreError(
                "project_relocation_stale",
                "Project relocation old path does not match every active binding",
            )
        if any(
            binding.lifecycle_status != "ARCHIVED"
            and binding.project_id != project_id
            and binding.canonical_cwd == new_cwd
            for binding in store.bindings.values()
        ):
            raise RuntimeControlStoreError(
                "project_relocation_collision",
                "Project relocation target is already owned by another Project",
            )
        binding_ids = tuple(sorted(binding.binding_id for binding in project_bindings))
        agent_instance_ids = tuple(
            sorted({binding.agent_instance_id for binding in project_bindings})
        )
        record = ProjectRelocationRecord(
            relocation_id=normalized_id,
            project_id=project_id,
            old_canonical_cwd=old_cwd,
            new_canonical_cwd=new_cwd,
            binding_ids=binding_ids,
            pending_agent_instance_ids=agent_instance_ids,
            created_at=datetime.now(timezone.utc),
        )
        updated = store.model_copy(deep=True)
        for binding in project_bindings:
            desired = binding.desired
            digest_payload = {
                **desired.digest_payload(),
                "canonical_cwd": new_cwd,
                "revision": desired.revision + 1,
            }
            relocated_desired = DesiredSpecRecord.model_validate(
                {
                    **digest_payload,
                    "spec_digest": canonical_digest(digest_payload),
                    "source_kind": "project_relocation",
                    "source_ref": normalized_id,
                }
            )
            updated.bindings[binding.binding_id] = binding.model_copy(
                update={"canonical_cwd": new_cwd, "desired": relocated_desired}
            )
        updated.project_relocations[normalized_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated, record


def acknowledge_project_relocation(
    path: Path,
    *,
    relocation_id: str,
    agent_instance_id: str,
) -> ProjectRelocationRecord:
    """Persist one Runtime namespace ACK idempotently after its snapshot succeeds."""

    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.project_relocations.get(relocation_id)
        if record is None or agent_instance_id not in record.pending_agent_instance_ids:
            raise RuntimeControlStoreError(
                "project_relocation_unknown",
                "Project relocation does not include this AgentInstance",
            )
        if agent_instance_id in record.acknowledged_agent_instance_ids:
            return record
        acknowledged = tuple(sorted((*record.acknowledged_agent_instance_ids, agent_instance_id)))
        completed = record.model_copy(update={"acknowledged_agent_instance_ids": acknowledged})
        updated = store.model_copy(deep=True)
        updated.project_relocations[relocation_id] = completed
        updated.store_revision += 1
        _write(safe_path, updated)
        return completed


def apply_runtime_snapshot(
    path: Path,
    *,
    agent_instance_id: str,
    generation_id: str,
    records: tuple[RuntimeBindingRecord, ...],
) -> RuntimeControlStore:
    """Atomically commit a complete CUS desired snapshot for one AgentInstance."""

    if not generation_id or not records:
        raise RuntimeControlStoreError(
            "invalid_runtime_snapshot",
            "Runtime snapshot requires a generation and at least one binding",
        )
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        agent = store.agent_instances.get(agent_instance_id)
        if agent is None or not agent.active:
            raise RuntimeControlStoreError(
                "agent_instance_unavailable",
                "Snapshot has no active AgentInstance",
            )
        incoming_ids: set[str] = set()
        topics: set[tuple[int, int]] = set()
        for record in records:
            if record.agent_instance_id != agent_instance_id:
                raise RuntimeControlStoreError(
                    "invalid_runtime_snapshot",
                    "Snapshot binding AgentInstance mismatch",
                )
            if record.chat_id != agent.forum_chat_id:
                raise RuntimeControlStoreError(
                    "agent_instance_unavailable",
                    "Snapshot binding forum chat mismatch",
                )
            if record.lifecycle_status == "UNCONFIGURED":
                raise RuntimeControlStoreError(
                    "invalid_runtime_snapshot",
                    "Unconfigured bindings cannot be part of a Runtime snapshot",
                )
            if (
                record.lifecycle_status in {"CONFIGURED", "UNMANAGED"}
                and record.desired.generation_id != generation_id
            ):
                raise RuntimeControlStoreError(
                    "invalid_runtime_snapshot",
                    "Configured desired generation does not match snapshot",
                )
            topic = (record.chat_id, record.thread_id)
            if record.binding_id in incoming_ids or topic in topics:
                raise RuntimeControlStoreError(
                    "invalid_runtime_snapshot",
                    "Snapshot contains duplicate binding or topic identity",
                )
            incoming_ids.add(record.binding_id)
            topics.add(topic)
            existing = store.bindings.get(record.binding_id)
            if existing is not None:
                if (
                    existing.agent_instance_id != record.agent_instance_id
                    or existing.chat_id != record.chat_id
                    or existing.thread_id != record.thread_id
                    or existing.project_id != record.project_id
                ):
                    raise RuntimeControlStoreError(
                        "binding_identity_changed",
                        "Runtime Binding identity is immutable",
                    )
                if existing.canonical_cwd != record.canonical_cwd:
                    raise RuntimeControlStoreError(
                        "project_relocation_required",
                        "Project path change requires explicit relocation",
                    )

        known_ids = {
            binding_id
            for binding_id, binding in store.bindings.items()
            if binding.agent_instance_id == agent_instance_id
        }
        omitted = known_ids - incoming_ids
        if omitted:
            raise RuntimeControlStoreError(
                "runtime_snapshot_incomplete",
                "Snapshot omits existing bindings; include them explicitly in their lifecycle",
            )

        updated = store.model_copy(deep=True)
        for record in records:
            updated.bindings[record.binding_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated


def commit_observed_proposal(
    path: Path,
    *,
    event_id: str,
    payload_digest: str,
    proposal: ObservedSpecProposalRecord,
    policy_allows: Callable[[RuntimeBindingRecord, ObservedSpecProposalRecord], bool],
) -> tuple[
    Literal["ACKED", "STALE_OBSERVED", "POLICY_REJECTED"],
    RuntimeBindingRecord | None,
]:
    """Commit one Runtime proposal exactly once under the CUS store lock.

    The policy callback must be local, deterministic, and side-effect free.
    """

    if event_id != proposal.expected_event_id:
        raise RuntimeControlStoreError(
            "runtime_event_identity_mismatch",
            "Observed event id does not match its immutable identity",
        )
    if payload_digest != canonical_digest(proposal.model_dump()):
        raise RuntimeControlStoreError(
            "runtime_event_digest_mismatch",
            "Observed event payload digest is invalid",
        )
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        receipt = store.runtime_event_receipts.get(event_id)
        if receipt is not None:
            if receipt.payload_digest != payload_digest:
                raise RuntimeControlStoreError(
                    "runtime_event_reused",
                    "Runtime event id was reused with different payload",
                )
            binding = store.bindings.get(proposal.binding_id)
            replayable_binding = (
                binding
                if receipt.ack_state == "ACKED"
                and binding is not None
                and binding.desired.source_kind == "observed_runtime"
                and binding.desired.source_ref == event_id
                else None
            )
            return receipt.ack_state, replayable_binding

        binding = store.bindings.get(proposal.binding_id)
        if binding is None or binding.lifecycle_status != "CONFIGURED":
            raise RuntimeControlStoreError(
                "runtime_binding_unavailable",
                "Observed proposal has no configured CUS Runtime Binding",
            )
        desired = binding.desired
        base_matches = (
            desired.generation_id == proposal.base_generation_id
            and desired.revision == proposal.base_revision
            and desired.spec_digest == proposal.base_spec_digest
        )
        updated = store.model_copy(deep=True)
        if not base_matches:
            ack_state: Literal["ACKED", "STALE_OBSERVED", "POLICY_REJECTED"] = "STALE_OBSERVED"
            committed_binding = None
        elif not policy_allows(binding, proposal):
            ack_state = "POLICY_REJECTED"
            committed_binding = None
        else:
            digest_payload = {
                **desired.digest_payload(),
                "revision": desired.revision + 1,
                "model": proposal.model,
                "reasoning_effort": proposal.reasoning_effort,
            }
            next_desired = DesiredSpecRecord(
                **digest_payload,
                spec_digest=canonical_digest(digest_payload),
                source_kind="observed_runtime",
                source_ref=event_id,
            )
            committed_binding = binding.model_copy(update={"desired": next_desired})
            updated.bindings[binding.binding_id] = committed_binding
            ack_state = "ACKED"
        updated.runtime_event_receipts[event_id] = RuntimeEventReceipt(
            event_id=event_id,
            payload_digest=payload_digest,
            ack_state=ack_state,
            processed_at=datetime.now(timezone.utc),
        )
        updated.store_revision += 1
        _write(safe_path, updated)
        return ack_state, committed_binding


def reserve_runtime_notification(
    path: Path,
    *,
    event_id: str,
    payload_digest: str,
    notification: RuntimeUserNotificationRecord,
) -> tuple[RuntimeNotificationReceipt, RuntimeBindingRecord, bool]:
    """Reserve one exact Runtime notification before its external side effect."""

    if not event_id.startswith("notice:"):
        raise RuntimeControlStoreError(
            "runtime_event_identity_mismatch",
            "Runtime notification event id is invalid",
        )
    if payload_digest != canonical_digest(notification.model_dump(by_alias=True)):
        raise RuntimeControlStoreError(
            "runtime_event_digest_mismatch",
            "Runtime notification payload digest is invalid",
        )
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        binding = store.bindings.get(notification.binding_id)
        if binding is None or binding.lifecycle_status == "ARCHIVED":
            raise RuntimeControlStoreError(
                "runtime_binding_unavailable",
                "Runtime notification has no active CUS Runtime Binding",
            )
        if (
            binding.agent_instance_id != notification.agent_instance_id
            or binding.chat_id != notification.chat_id
            or binding.thread_id != notification.thread_id
        ):
            raise RuntimeControlStoreError(
                "runtime_event_namespace_mismatch",
                "Runtime notification target does not match its CUS binding",
            )
        existing = store.runtime_notification_receipts.get(event_id)
        if existing is not None:
            if existing.payload_digest != payload_digest:
                raise RuntimeControlStoreError(
                    "runtime_event_reused",
                    "Runtime event id was reused with different payload",
                )
            return existing, binding, False
        receipt = RuntimeNotificationReceipt(
            event_id=event_id,
            payload_digest=payload_digest,
            state="RESERVED",
            updated_at=datetime.now(timezone.utc),
        )
        updated = store.model_copy(deep=True)
        updated.runtime_notification_receipts[event_id] = receipt
        updated.store_revision += 1
        _write(safe_path, updated)
        return receipt, binding, True


def finish_runtime_notification(
    path: Path,
    *,
    event_id: str,
    state: Literal["DELIVERED", "DELIVERY_UNKNOWN", "REJECTED"],
    message_id: int | None = None,
) -> RuntimeNotificationReceipt:
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        receipt = store.runtime_notification_receipts.get(event_id)
        if receipt is None:
            raise RuntimeControlStoreError(
                "runtime_notification_unreserved",
                "Runtime notification has no delivery reservation",
            )
        if receipt.state != "RESERVED":
            if receipt.state != state or receipt.message_id != message_id:
                raise RuntimeControlStoreError(
                    "runtime_notification_already_final",
                    "Runtime notification already has another final state",
                )
            return receipt
        completed = receipt.model_copy(
            update={
                "state": state,
                "message_id": message_id,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        # model_copy does not re-run validators.
        completed = RuntimeNotificationReceipt.model_validate(completed.model_dump())
        updated = store.model_copy(deep=True)
        updated.runtime_notification_receipts[event_id] = completed
        updated.store_revision += 1
        _write(safe_path, updated)
        return completed


def release_runtime_notification_reservation(path: Path, *, event_id: str) -> None:
    """Release only a definitely-unsent notification so Runtime can retry it."""

    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        receipt = store.runtime_notification_receipts.get(event_id)
        if receipt is None:
            return
        if receipt.state != "RESERVED":
            raise RuntimeControlStoreError(
                "runtime_notification_already_final",
                "Final Runtime notification cannot be released",
            )
        updated = store.model_copy(deep=True)
        del updated.runtime_notification_receipts[event_id]
        updated.store_revision += 1
        _write(safe_path, updated)
