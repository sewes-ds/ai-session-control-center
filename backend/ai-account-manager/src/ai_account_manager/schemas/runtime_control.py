from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class AgentInstanceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    pinned_upstream_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    telegram_bot_identity: str = Field(min_length=1, max_length=200)
    telegram_bot_token_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    forum_chat_id: int = Field(lt=0)
    runtime_socket: str
    active: bool = True


class DesiredSpecRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=128)
    agent_instance_id: str = Field(min_length=1, max_length=64)
    generation_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=128)
    canonical_cwd: str
    provider: str = Field(min_length=1, max_length=64)
    account_profile: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)
    source_kind: Literal[
        "cus_ui",
        "observed_runtime",
        "migration",
        "snapshot",
        "project_relocation",
    ]
    source_ref: str = Field(min_length=1, max_length=256)

    def digest_payload(self) -> dict[str, object]:
        return {
            "account_profile": self.account_profile,
            "agent_instance_id": self.agent_instance_id,
            "binding_id": self.binding_id,
            "canonical_cwd": self.canonical_cwd,
            "generation_id": self.generation_id,
            "model": self.model,
            "project_id": self.project_id,
            "provider": self.provider,
            "reasoning_effort": self.reasoning_effort,
            "revision": self.revision,
        }

    @model_validator(mode="after")
    def validate_digest(self) -> DesiredSpecRecord:
        if canonical_digest(self.digest_payload()) != self.spec_digest:
            raise ValueError("spec_digest does not match canonical desired state")
        return self

    def runtime_payload(self) -> dict[str, object]:
        return {
            **self.digest_payload(),
            "spec_digest": self.spec_digest,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
        }


class RuntimeBindingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["telegram_topic"] = "telegram_topic"
    agent_instance_id: str = Field(min_length=1, max_length=64)
    chat_id: int = Field(lt=0)
    thread_id: int = Field(gt=0)
    topic_name: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=128)
    canonical_cwd: str
    desired: DesiredSpecRecord
    lifecycle_status: Literal["UNCONFIGURED", "CONFIGURED", "UNMANAGED", "ARCHIVED"] = "CONFIGURED"

    @model_validator(mode="after")
    def validate_desired_identity(self) -> RuntimeBindingRecord:
        desired = self.desired
        if (
            desired.binding_id != self.binding_id
            or desired.agent_instance_id != self.agent_instance_id
            or desired.project_id != self.project_id
            or desired.canonical_cwd != self.canonical_cwd
        ):
            raise ValueError("desired spec identity does not match Runtime Binding")
        return self

    def runtime_binding_payload(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "agent_instance_id": self.agent_instance_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "canonical_cwd": self.canonical_cwd,
        }


class ProjectRelocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relocation_id: str = Field(pattern=r"^rel_[0-9a-f]{32}$")
    project_id: str = Field(min_length=1, max_length=128)
    old_canonical_cwd: str
    new_canonical_cwd: str
    binding_ids: tuple[str, ...] = Field(min_length=1)
    pending_agent_instance_ids: tuple[str, ...] = Field(min_length=1)
    acknowledged_agent_instance_ids: tuple[str, ...] = ()
    created_at: datetime

    @model_validator(mode="after")
    def validate_relocation(self) -> ProjectRelocationRecord:
        if self.old_canonical_cwd == self.new_canonical_cwd:
            raise ValueError("project relocation must change canonical cwd")
        for value in (self.old_canonical_cwd, self.new_canonical_cwd):
            if not value.startswith("/") or "/../" in f"{value}/" or value == "/":
                raise ValueError("project relocation paths must be absolute and traversal-free")
        if len(self.binding_ids) != len(set(self.binding_ids)):
            raise ValueError("project relocation binding ids must be unique")
        if len(self.pending_agent_instance_ids) != len(set(self.pending_agent_instance_ids)):
            raise ValueError("project relocation pending AgentInstances must be unique")
        if len(self.acknowledged_agent_instance_ids) != len(
            set(self.acknowledged_agent_instance_ids)
        ):
            raise ValueError("project relocation acknowledged AgentInstances must be unique")
        if not set(self.acknowledged_agent_instance_ids) <= set(self.pending_agent_instance_ids):
            raise ValueError("project relocation ACK is outside the pending AgentInstances")
        return self

    @property
    def complete(self) -> bool:
        return set(self.acknowledged_agent_instance_ids) == set(self.pending_agent_instance_ids)

    def runtime_payload(
        self,
        *,
        agent_instance_id: str,
        binding_ids: tuple[str, ...],
    ) -> dict[str, object]:
        if agent_instance_id not in self.pending_agent_instance_ids:
            raise ValueError("AgentInstance is not part of this project relocation")
        selected = tuple(sorted(binding_ids))
        if not selected or not set(selected) <= set(self.binding_ids):
            raise ValueError("Runtime relocation binding ids are invalid")
        return {
            "relocation_id": self.relocation_id,
            "agent_instance_id": agent_instance_id,
            "project_id": self.project_id,
            "old_canonical_cwd": self.old_canonical_cwd,
            "new_canonical_cwd": self.new_canonical_cwd,
            "binding_ids": list(selected),
        }


class ObservedSpecProposalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=128)
    base_generation_id: str = Field(min_length=1, max_length=128)
    base_revision: int = Field(ge=1)
    base_spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)
    instance_id: str = Field(min_length=1, max_length=128)
    observation_sequence: int = Field(ge=0)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def expected_event_id(self) -> str:
        identity: dict[str, object] = {
            "binding_id": self.binding_id,
            "instance_id": self.instance_id,
            "observation_sequence": self.observation_sequence,
            "evidence_digest": self.evidence_digest,
        }
        return f"observed:{canonical_digest(identity)[7:]}"


class RuntimeUserNotificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["ai-agent-runtime.user-notification.v1"] = Field(alias="schema")
    notification_code: Literal[
        "ONBOARDING_REQUIRED",
        "RESUME_BINDING_MISMATCH",
        "UNSUPPORTED_CODEX_SUBPROCESS",
    ]
    agent_instance_id: str = Field(min_length=1, max_length=64)
    binding_id: str = Field(min_length=1, max_length=128)
    chat_id: int = Field(lt=0)
    thread_id: int = Field(gt=0)


class RuntimeEventReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    payload_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ack_state: Literal["ACKED", "STALE_OBSERVED", "POLICY_REJECTED"]
    processed_at: datetime


class RuntimeNotificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    payload_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["RESERVED", "DELIVERED", "DELIVERY_UNKNOWN", "REJECTED"]
    message_id: int | None = Field(default=None, gt=0)
    updated_at: datetime

    @model_validator(mode="after")
    def validate_delivery_evidence(self) -> RuntimeNotificationReceipt:
        if (self.state == "DELIVERED") != (self.message_id is not None):
            raise ValueError("only delivered notifications may have a message id")
        return self


class OnboardingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_id: str = Field(pattern=r"^onb_[0-9a-f]{32}$")
    token_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    previous_token_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    agent_instance_id: str = Field(min_length=1, max_length=64)
    chat_id: int = Field(lt=0)
    thread_id: int = Field(gt=0)
    topic_name: str = Field(min_length=1, max_length=256)
    state: Literal["UNCONFIGURED", "CONFIGURED"] = "UNCONFIGURED"
    binding_id: str | None = Field(default=None, min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    configured_at: datetime | None = None
    setup_message_id: int | None = Field(default=None, gt=0)
    setup_notified_at: datetime | None = None
    setup_button_token_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    completion_message_id: int | None = Field(default=None, gt=0)
    completion_notified_at: datetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> OnboardingRecord:
        if self.state == "CONFIGURED" and (self.binding_id is None or self.configured_at is None):
            raise ValueError("configured onboarding requires binding and timestamp")
        if self.expires_at <= self.created_at:
            raise ValueError("onboarding token must expire after creation")
        if (self.setup_message_id is None) != (self.setup_notified_at is None):
            raise ValueError("setup notification id and timestamp must be set together")
        if (self.completion_message_id is None) != (self.completion_notified_at is None):
            raise ValueError("completion notification id and timestamp must be set together")
        return self


class RuntimeControlStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    store_revision: int = Field(ge=1)
    agent_instances: dict[str, AgentInstanceRecord] = Field(default_factory=dict)
    bindings: dict[str, RuntimeBindingRecord] = Field(default_factory=dict)
    project_relocations: dict[str, ProjectRelocationRecord] = Field(default_factory=dict)
    runtime_event_receipts: dict[str, RuntimeEventReceipt] = Field(default_factory=dict)
    runtime_notification_receipts: dict[str, RuntimeNotificationReceipt] = Field(
        default_factory=dict
    )
    onboarding_records: dict[str, OnboardingRecord] = Field(default_factory=dict)
