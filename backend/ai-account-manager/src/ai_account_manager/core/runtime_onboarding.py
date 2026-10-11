from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    _locked,
    _read,
    _validate_path,
    _write,
)
from ai_account_manager.schemas.runtime_control import (
    DesiredSpecRecord,
    OnboardingRecord,
    RuntimeBindingRecord,
    canonical_digest,
)


class RuntimeOnboardingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class IssuedOnboardingToken:
    record: OnboardingRecord
    token: str


@dataclass(frozen=True)
class PreparedTopicOnboarding:
    record: OnboardingRecord
    binding: RuntimeBindingRecord
    runtime_socket: Path


def _token_hash(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _onboarding_id(agent_instance_id: str, chat_id: int, thread_id: int) -> str:
    source = f"{agent_instance_id}\0{chat_id}\0{thread_id}".encode()
    return f"onb_{hashlib.sha256(source).hexdigest()[:32]}"


def issue_topic_onboarding_token(
    path: Path,
    *,
    chat_id: int,
    thread_id: int,
    topic_name: str,
    ttl_seconds: int,
    now: datetime | None = None,
    token_factory: Callable[[], str] | None = None,
) -> IssuedOnboardingToken:
    """Create or rotate a short-lived topic-scoped token.

    The raw token is returned once to the CUS Bot delivery layer. Only its digest is
    stored in the OnboardingRecord. This token selects a route; it is not user auth.
    """

    if chat_id >= 0 or thread_id <= 0:
        raise RuntimeOnboardingError("INVALID_TOPIC", "Invalid Telegram forum topic")
    clean_name = topic_name.strip()
    if not clean_name or len(clean_name) > 256:
        raise RuntimeOnboardingError("INVALID_TOPIC", "Invalid Telegram topic name")
    if ttl_seconds < 60 or ttl_seconds > 86400:
        raise RuntimeOnboardingError(
            "INVALID_TOKEN_TTL", "Onboarding token TTL must be between 60 and 86400 seconds"
        )
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        agents = [
            agent
            for agent in store.agent_instances.values()
            if agent.active and agent.forum_chat_id == chat_id
        ]
        if len(agents) != 1:
            raise RuntimeOnboardingError(
                "AGENT_INSTANCE_AMBIGUOUS",
                "Forum chat must resolve to exactly one active AgentInstance",
            )
        agent = agents[0]
        for binding in store.bindings.values():
            if (
                binding.agent_instance_id == agent.agent_instance_id
                and binding.chat_id == chat_id
                and binding.thread_id == thread_id
                and binding.lifecycle_status != "ARCHIVED"
            ):
                raise RuntimeOnboardingError(
                    "TOPIC_ALREADY_BOUND", "Telegram topic already has a Runtime Binding"
                )
        token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
        if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
            raise RuntimeOnboardingError("INVALID_TOKEN", "Generated onboarding token is invalid")
        onboarding_id = _onboarding_id(agent.agent_instance_id, chat_id, thread_id)
        existing = store.onboarding_records.get(onboarding_id)
        if existing is not None and existing.setup_message_id is not None:
            # A replayed Telegram update must not send a second setup notice.
            # The original raw token is intentionally not stored or returned.
            return IssuedOnboardingToken(record=existing, token="")
        record = OnboardingRecord(
            onboarding_id=onboarding_id,
            token_hash=_token_hash(token),
            agent_instance_id=agent.agent_instance_id,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_name=clean_name,
            state="UNCONFIGURED",
            created_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return IssuedOnboardingToken(record=record, token=token)


def mark_setup_notice_delivered(
    path: Path,
    *,
    onboarding_id: str,
    token: str,
    message_id: int,
    now: datetime | None = None,
) -> OnboardingRecord:
    notified_at = now or datetime.now(UTC)
    if notified_at.tzinfo is None or message_id <= 0:
        raise RuntimeOnboardingError("INVALID_NOTIFICATION", "Invalid setup notification evidence")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.onboarding_records.get(onboarding_id)
        if record is None or not hmac.compare_digest(record.token_hash, _token_hash(token)):
            raise RuntimeOnboardingError("ONBOARDING_STATE_CHANGED", "Onboarding token changed")
        if record.setup_message_id is not None:
            if record.setup_message_id == message_id:
                return record
            raise RuntimeOnboardingError(
                "SETUP_NOTICE_CONFLICT", "Setup notice already has different delivery evidence"
            )
        updated_record = record.model_copy(
            update={
                "setup_message_id": message_id,
                "setup_notified_at": notified_at,
                "setup_button_token_hash": record.token_hash,
            }
        )
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = OnboardingRecord.model_validate(
            updated_record.model_dump()
        )
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated.onboarding_records[onboarding_id]


def renew_binding_onboarding_token(
    path: Path,
    *,
    binding_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
    token_factory: Callable[[], str] | None = None,
) -> IssuedOnboardingToken:
    """Rotate the setup link for an existing unconfigured Runtime Binding."""

    if ttl_seconds < 60 or ttl_seconds > 86400:
        raise RuntimeOnboardingError(
            "INVALID_TOKEN_TTL", "Onboarding token TTL must be between 60 and 86400 seconds"
        )
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    token = (token_factory or (lambda: secrets.token_urlsafe(32)))()
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise RuntimeOnboardingError("INVALID_TOKEN", "Generated onboarding token is invalid")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        binding = store.bindings.get(binding_id)
        if binding is None or binding.lifecycle_status != "UNCONFIGURED":
            raise RuntimeOnboardingError(
                "BINDING_NOT_AWAITING_ONBOARDING",
                "Runtime Binding is not awaiting onboarding",
            )
        agent = store.agent_instances.get(binding.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != binding.chat_id:
            raise RuntimeOnboardingError(
                "AGENT_INSTANCE_UNAVAILABLE", "Onboarding AgentInstance is unavailable"
            )
        onboarding_id = _onboarding_id(
            binding.agent_instance_id,
            binding.chat_id,
            binding.thread_id,
        )
        existing = store.onboarding_records.get(onboarding_id)
        if existing is not None and existing.state != "UNCONFIGURED":
            raise RuntimeOnboardingError(
                "ONBOARDING_STATE_CHANGED", "Onboarding is already configured"
            )
        record = OnboardingRecord(
            onboarding_id=onboarding_id,
            token_hash=_token_hash(token),
            agent_instance_id=binding.agent_instance_id,
            chat_id=binding.chat_id,
            thread_id=binding.thread_id,
            topic_name=binding.topic_name,
            state="UNCONFIGURED",
            binding_id=binding.binding_id,
            created_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = record
        updated.store_revision += 1
        _write(safe_path, updated)
        return IssuedOnboardingToken(record=record, token=token)


def resolve_topic_onboarding_token(
    path: Path,
    token: str,
    *,
    now: datetime | None = None,
) -> OnboardingRecord:
    if not token or len(token) > 512:
        raise RuntimeOnboardingError("ONBOARDING_TOKEN_INVALID", "Onboarding token is invalid")
    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    try:
        safe_path = _validate_path(path)
        with _locked(safe_path):
            store = _read(safe_path)
    except RuntimeControlStoreError as exc:
        raise RuntimeOnboardingError(exc.code, exc.message) from exc
    expected_hash = _token_hash(token)
    matches = [
        record
        for record in store.onboarding_records.values()
        if hmac.compare_digest(record.token_hash, expected_hash)
    ]
    if len(matches) != 1:
        raise RuntimeOnboardingError("ONBOARDING_TOKEN_INVALID", "Onboarding token is invalid")
    record = matches[0]
    if checked_at >= record.expires_at:
        raise RuntimeOnboardingError("ONBOARDING_TOKEN_EXPIRED", "Onboarding token has expired")
    if record.state == "CONFIGURED":
        return record
    return record


def find_recoverable_topic_onboarding_token(path: Path, token: str) -> OnboardingRecord:
    """Find an expired setup link for authorization; never authorize with it alone."""

    if not token or len(token) > 512:
        raise RuntimeOnboardingError("ONBOARDING_TOKEN_INVALID", "Onboarding token is invalid")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
    expected_hash = _token_hash(token)
    matches = [
        record
        for record in store.onboarding_records.values()
        if record.state == "UNCONFIGURED"
        and record.setup_message_id is not None
        and (
            hmac.compare_digest(record.token_hash, expected_hash)
            or (
                record.previous_token_hash is not None
                and hmac.compare_digest(record.previous_token_hash, expected_hash)
            )
        )
    ]
    if len(matches) != 1:
        raise RuntimeOnboardingError("ONBOARDING_TOKEN_INVALID", "Onboarding token is invalid")
    return matches[0]


def renew_expired_topic_onboarding_token(
    path: Path,
    *,
    old_token: str,
    token_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedOnboardingToken:
    """Rotate an expired delivered link, preserving the original setup message.

    The replacement is derivable from the previous bearer token and stored issue
    time. Thus retry after an uncertain Telegram edit uses the same URL, without
    persisting plaintext tokens or creating a second setup notice.
    """

    if len(token_key.encode()) < 32 or not old_token or len(old_token) > 512:
        raise RuntimeOnboardingError("INVALID_TOKEN", "Onboarding renewal key/token is invalid")
    if ttl_seconds < 60 or ttl_seconds > 86400:
        raise RuntimeOnboardingError("INVALID_TOKEN_TTL", "Invalid onboarding token TTL")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    safe_path = _validate_path(path)
    supplied_hash = _token_hash(old_token)
    with _locked(safe_path):
        store = _read(safe_path)
        matches = [
            record
            for record in store.onboarding_records.values()
            if record.state == "UNCONFIGURED"
            and record.setup_message_id is not None
            and (
                hmac.compare_digest(record.token_hash, supplied_hash)
                or (
                    record.previous_token_hash is not None
                    and hmac.compare_digest(record.previous_token_hash, supplied_hash)
                )
            )
        ]
        if len(matches) != 1:
            raise RuntimeOnboardingError("ONBOARDING_TOKEN_INVALID", "Onboarding token is invalid")
        record = matches[0]
        agent = store.agent_instances.get(record.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != record.chat_id:
            raise RuntimeOnboardingError(
                "AGENT_INSTANCE_UNAVAILABLE", "AgentInstance is unavailable"
            )
        if any(
            binding.agent_instance_id == record.agent_instance_id
            and binding.chat_id == record.chat_id
            and binding.thread_id == record.thread_id
            and binding.lifecycle_status not in ("ARCHIVED", "UNCONFIGURED")
            for binding in store.bindings.values()
        ):
            raise RuntimeOnboardingError("TOPIC_ALREADY_BOUND", "Topic is already configured")
        if record.previous_token_hash is not None and hmac.compare_digest(
            record.previous_token_hash, supplied_hash
        ):
            if issued_at >= record.expires_at:
                raise RuntimeOnboardingError(
                    "ONBOARDING_TOKEN_EXPIRED", "Reopen the latest setup link"
                )
            replacement = _derived_replacement_token(
                token_key, record.previous_token_hash, record.created_at
            )
            if not hmac.compare_digest(_token_hash(replacement), record.token_hash):
                raise RuntimeOnboardingError("ONBOARDING_STATE_CHANGED", "Onboarding token changed")
            return IssuedOnboardingToken(record=record, token=replacement)
        if issued_at < record.expires_at:
            return IssuedOnboardingToken(record=record, token=old_token)
        replacement = _derived_replacement_token(token_key, record.token_hash, issued_at)
        renewed = OnboardingRecord.model_validate(
            record.model_copy(
                update={
                    "previous_token_hash": record.token_hash,
                    "token_hash": _token_hash(replacement),
                    "created_at": issued_at,
                    "expires_at": issued_at + timedelta(seconds=ttl_seconds),
                }
            ).model_dump()
        )
        updated = store.model_copy(deep=True)
        updated.onboarding_records[record.onboarding_id] = renewed
        updated.store_revision += 1
        _write(safe_path, updated)
        return IssuedOnboardingToken(record=renewed, token=replacement)


def renew_expired_topic_onboarding_record(
    path: Path,
    *,
    onboarding_id: str,
    token_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedOnboardingToken | None:
    """Background renewal for a delivered notice; None means not yet expired."""

    if len(token_key.encode()) < 32 or not 60 <= ttl_seconds <= 86400:
        raise RuntimeOnboardingError("INVALID_TOKEN", "Onboarding renewal settings are invalid")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.onboarding_records.get(onboarding_id)
        if record is None or record.state != "UNCONFIGURED" or record.setup_message_id is None:
            return None
        if issued_at < record.expires_at:
            if record.setup_button_token_hash == record.token_hash:
                return None
            if record.previous_token_hash is None:
                return None
            replacement = _derived_replacement_token(
                token_key, record.previous_token_hash, record.created_at
            )
            if not hmac.compare_digest(_token_hash(replacement), record.token_hash):
                raise RuntimeOnboardingError("ONBOARDING_STATE_CHANGED", "Onboarding token changed")
            return IssuedOnboardingToken(record=record, token=replacement)
        agent = store.agent_instances.get(record.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != record.chat_id:
            raise RuntimeOnboardingError(
                "AGENT_INSTANCE_UNAVAILABLE", "AgentInstance is unavailable"
            )
        if any(
            binding.agent_instance_id == record.agent_instance_id
            and binding.chat_id == record.chat_id
            and binding.thread_id == record.thread_id
            and binding.lifecycle_status not in ("ARCHIVED", "UNCONFIGURED")
            for binding in store.bindings.values()
        ):
            return None
        replacement = _derived_replacement_token(token_key, record.token_hash, issued_at)
        renewed = OnboardingRecord.model_validate(
            record.model_copy(
                update={
                    "previous_token_hash": record.token_hash,
                    "token_hash": _token_hash(replacement),
                    "created_at": issued_at,
                    "expires_at": issued_at + timedelta(seconds=ttl_seconds),
                }
            ).model_dump()
        )
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = renewed
        updated.store_revision += 1
        _write(safe_path, updated)
        return IssuedOnboardingToken(record=renewed, token=replacement)


def mark_setup_button_refreshed(
    path: Path,
    *,
    onboarding_id: str,
    token: str,
    message_id: int,
) -> OnboardingRecord:
    """Acknowledge the link that Telegram currently displays on the setup message."""

    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.onboarding_records.get(onboarding_id)
        expected_hash = _token_hash(token)
        if (
            record is None
            or record.setup_message_id != message_id
            or not hmac.compare_digest(record.token_hash, expected_hash)
        ):
            raise RuntimeOnboardingError("ONBOARDING_STATE_CHANGED", "Setup button target changed")
        if record.setup_button_token_hash == expected_hash:
            return record
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = record.model_copy(
            update={"setup_button_token_hash": expected_hash}
        )
        updated.store_revision += 1
        _write(safe_path, updated)
        return updated.onboarding_records[onboarding_id]


def _derived_replacement_token(token_key: str, prior_hash: str, issued_at: datetime) -> str:
    material = f"onboarding-renew-v1\0{prior_hash}\0{issued_at.isoformat()}".encode()
    return hmac.new(token_key.encode(), material, hashlib.sha256).hexdigest()


def _binding_id(agent_instance_id: str, chat_id: int, thread_id: int) -> str:
    source = f"{agent_instance_id}\0{chat_id}\0{thread_id}".encode()
    return f"rb_{hashlib.sha256(source).hexdigest()[:32]}"


def prepare_topic_onboarding_binding(
    path: Path,
    *,
    onboarding_id: str,
    token: str,
    project_id: str,
    canonical_cwd: str,
    provider: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    source_ref: str,
    now: datetime | None = None,
    generation_factory: Callable[[], str] | None = None,
) -> PreparedTopicOnboarding:
    """Durably prepare desired state before any Runtime side effect."""

    checked_at = now or datetime.now(UTC)
    record = resolve_topic_onboarding_token(path, token, now=checked_at)
    if record.onboarding_id != onboarding_id:
        raise RuntimeOnboardingError(
            "ONBOARDING_ID_MISMATCH", "Onboarding token does not match this operation"
        )
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        current = store.onboarding_records.get(onboarding_id)
        if (
            current is None
            or not hmac.compare_digest(current.token_hash, _token_hash(token))
            or current.state != "UNCONFIGURED"
            or checked_at >= current.expires_at
        ):
            raise RuntimeOnboardingError(
                "ONBOARDING_STATE_CHANGED", "Onboarding state changed before apply"
            )
        agent = store.agent_instances.get(current.agent_instance_id)
        if agent is None or not agent.active or agent.forum_chat_id != current.chat_id:
            raise RuntimeOnboardingError(
                "AGENT_INSTANCE_UNAVAILABLE", "Onboarding AgentInstance is unavailable"
            )
        binding_id = _binding_id(
            current.agent_instance_id,
            current.chat_id,
            current.thread_id,
        )
        existing = store.bindings.get(binding_id)
        if any(
            item.binding_id != binding_id
            and item.agent_instance_id == current.agent_instance_id
            and item.chat_id == current.chat_id
            and item.thread_id == current.thread_id
            and item.lifecycle_status != "ARCHIVED"
            for item in store.bindings.values()
        ):
            raise RuntimeOnboardingError(
                "TOPIC_ALREADY_BOUND", "Telegram topic already has another Runtime Binding"
            )
        if existing is not None and (
            existing.agent_instance_id != current.agent_instance_id
            or existing.chat_id != current.chat_id
            or existing.thread_id != current.thread_id
            or existing.project_id != project_id
            or existing.canonical_cwd != canonical_cwd
        ):
            raise RuntimeOnboardingError(
                "BINDING_IDENTITY_CHANGED", "Prepared Runtime Binding identity changed"
            )
        generations = {
            item.desired.generation_id
            for item in store.bindings.values()
            if item.agent_instance_id == current.agent_instance_id
            and item.lifecycle_status != "ARCHIVED"
        }
        if len(generations) > 1:
            raise RuntimeOnboardingError(
                "DESIRED_GENERATION_AMBIGUOUS",
                "AgentInstance bindings have inconsistent desired generations",
            )
        if existing is not None:
            generation_id = existing.desired.generation_id
            same_values = (
                existing.desired.provider == provider
                and existing.desired.account_profile == account_profile
                and existing.desired.model == model
                and existing.desired.reasoning_effort == reasoning_effort
            )
            revision = existing.desired.revision if same_values else existing.desired.revision + 1
        else:
            generation_id = (
                next(iter(generations), "")
                or (generation_factory or (lambda: f"gen_{uuid.uuid4().hex}"))()
            )
            revision = 1
        if not generation_id:
            raise RuntimeOnboardingError(
                "DESIRED_GENERATION_INVALID", "Desired generation is invalid"
            )
        digest_payload: dict[str, object] = {
            "binding_id": binding_id,
            "agent_instance_id": current.agent_instance_id,
            "generation_id": generation_id,
            "revision": revision,
            "project_id": project_id,
            "canonical_cwd": canonical_cwd,
            "provider": provider,
            "account_profile": account_profile,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
        desired = DesiredSpecRecord(
            **digest_payload,
            spec_digest=canonical_digest(digest_payload),
            source_kind="cus_ui",
            source_ref=source_ref,
        )
        binding = RuntimeBindingRecord(
            binding_id=binding_id,
            agent_instance_id=current.agent_instance_id,
            chat_id=current.chat_id,
            thread_id=current.thread_id,
            topic_name=current.topic_name,
            project_id=project_id,
            canonical_cwd=canonical_cwd,
            desired=desired,
            lifecycle_status="UNCONFIGURED",
        )
        updated = store.model_copy(deep=True)
        updated.bindings[binding_id] = binding
        updated.store_revision += 1
        _write(safe_path, updated)
        return PreparedTopicOnboarding(
            record=current,
            binding=binding,
            runtime_socket=Path(agent.runtime_socket),
        )


def finalize_topic_onboarding(
    path: Path,
    *,
    onboarding_id: str,
    binding_id: str,
    expected_spec_digest: str,
    now: datetime | None = None,
) -> tuple[OnboardingRecord, RuntimeBindingRecord]:
    """Atomically expose success only after Runtime acknowledged apply."""

    completed_at = now or datetime.now(UTC)
    if completed_at.tzinfo is None:
        raise RuntimeOnboardingError("INVALID_TIME", "Onboarding time must be timezone-aware")
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.onboarding_records.get(onboarding_id)
        binding = store.bindings.get(binding_id)
        if (
            record is None
            or binding is None
            or record.agent_instance_id != binding.agent_instance_id
            or record.chat_id != binding.chat_id
            or record.thread_id != binding.thread_id
        ):
            raise RuntimeOnboardingError(
                "ONBOARDING_STATE_CHANGED", "Prepared onboarding state is missing"
            )
        if binding.desired.spec_digest != expected_spec_digest:
            raise RuntimeOnboardingError(
                "ONBOARDING_STATE_CHANGED", "Prepared desired state changed before finalize"
            )
        if record.state == "CONFIGURED":
            if record.binding_id != binding_id:
                raise RuntimeOnboardingError(
                    "ONBOARDING_STATE_CHANGED", "Onboarding was finalized for another binding"
                )
            return record, binding
        configured_binding = binding.model_copy(update={"lifecycle_status": "CONFIGURED"})
        configured_record = record.model_copy(
            update={
                "state": "CONFIGURED",
                "binding_id": binding_id,
                "configured_at": completed_at,
            }
        )
        configured_record = OnboardingRecord.model_validate(configured_record.model_dump())
        updated = store.model_copy(deep=True)
        updated.bindings[binding_id] = configured_binding
        updated.onboarding_records[onboarding_id] = configured_record
        updated.store_revision += 1
        _write(safe_path, updated)
        return configured_record, configured_binding


def mark_onboarding_completion_notified(
    path: Path,
    *,
    onboarding_id: str,
    binding_id: str,
    message_id: int,
    now: datetime | None = None,
) -> OnboardingRecord:
    notified_at = now or datetime.now(UTC)
    if notified_at.tzinfo is None or message_id <= 0:
        raise RuntimeOnboardingError(
            "COMPLETION_EVIDENCE_INVALID", "Completion notification evidence is invalid"
        )
    safe_path = _validate_path(path)
    with _locked(safe_path):
        store = _read(safe_path)
        record = store.onboarding_records.get(onboarding_id)
        if record is None or record.state != "CONFIGURED" or record.binding_id != binding_id:
            raise RuntimeOnboardingError(
                "ONBOARDING_STATE_CHANGED", "Configured onboarding state is missing"
            )
        if record.completion_message_id is not None:
            return record
        notified = record.model_copy(
            update={
                "completion_message_id": message_id,
                "completion_notified_at": notified_at,
            }
        )
        notified = OnboardingRecord.model_validate(notified.model_dump())
        updated = store.model_copy(deep=True)
        updated.onboarding_records[onboarding_id] = notified
        updated.store_revision += 1
        _write(safe_path, updated)
        return notified
