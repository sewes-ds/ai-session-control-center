from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RuntimeHealthCode = Literal[
    "RUNTIME_HEALTHY",
    "DEGRADED_CUS_SYNC",
    "PENDING_CHANGE",
    "OUT_OF_SYNC",
    "UNMANAGED",
    "UNSUPPORTED_CODEX_SUBPROCESS",
    "OBSERVED_PENDING_SYNC",
    "OBSERVED_POLICY_REJECTED",
    "CONFIG_DRIFT",
    "CORRELATION_MISMATCH",
    "AMBIGUOUS",
    "ERROR",
    "UNCONFIGURED",
    "ARCHIVED",
    "RUNTIME_UNAVAILABLE",
]
RuntimeActivity = Literal["STOPPED", "IDLE", "BUSY", "UNKNOWN"]


class RuntimeRevisionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RuntimeDesiredPublic(RuntimeRevisionPublic):
    provider: str = Field(min_length=1, max_length=64)
    account_profile: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)


class RuntimeBindingHealthPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=128)
    agent_instance_id: str = Field(min_length=1, max_length=64)
    chat_id: int = Field(lt=0)
    thread_id: int = Field(gt=0)
    topic_name: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=128)
    lifecycle_status: Literal["UNCONFIGURED", "CONFIGURED", "UNMANAGED", "ARCHIVED"]
    health: RuntimeHealthCode
    management_mode: str | None = Field(default=None, max_length=64)
    runtime_available: bool
    desired: RuntimeDesiredPublic
    runtime_desired: RuntimeRevisionPublic | None = None
    applied: tuple[RuntimeRevisionPublic, ...] = ()
    active_instance_count: int = Field(ge=0)
    activity: RuntimeActivity
    diagnostic_code: str | None = Field(default=None, max_length=100)


class RuntimeAgentHealthPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_instance_id: str = Field(min_length=1, max_length=64)
    available: bool
    diagnostic_code: str | None = Field(default=None, max_length=100)
    unrecognized_binding_count: int = Field(ge=0)


class RuntimeBindingHealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindings: tuple[RuntimeBindingHealthPublic, ...]
    agent_instances: tuple[RuntimeAgentHealthPublic, ...]
    telegram_e2e_verified: Literal[False] = False
