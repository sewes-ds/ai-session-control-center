from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TelegramE2ERequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_binary: str
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: str
    cwd: str
    overlay_root: str
    project_state_path: str
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=80)
    final_marker: str = Field(pattern=r"^[A-Z0-9_-]{8,160}$")
    resume_marker: str = Field(pattern=r"^[A-Z0-9_-]{8,160}$")
    fixture_scenario: Literal["normal", "lazy", "missing", "unknown"] | None = None


class TelegramE2EResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    verdict: Literal["PASS", "CHECK_FAILED"]
    failure_codes: list[str]
    transport: Literal["IN_MEMORY_NON_NETWORK"] = "IN_MEMORY_NON_NETWORK"
    network_capable: Literal[False] = False
    queue_accepted: bool
    send_lifecycle_completed: bool
    final_marker_deliveries: int
    resume_marker_deliveries: int
    duplicate_final_suppressed: bool
    completion_without_final_rejected: bool
    unknown_schema_rejected: bool
    session_id: str | None
    resumed_session_id: str | None
    tmux_socket: str
    tmux_session_name: str
    pane_pid: int | None
    candidate_command: str
    candidate_argv: list[str]
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_model: str | None
    observed_reasoning_effort: str | None
    runtime_selection_status: str
    stopped_projects_started: bool
    project_state_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_state_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    telegram_import_origin: str
    rollout_import_origin: str
