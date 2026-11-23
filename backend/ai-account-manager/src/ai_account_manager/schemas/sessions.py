from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionQuotaWindowSummary(BaseModel):
    window_type: str
    window_label: str
    remaining_percent: float | None = None
    resets_at: str | int | None = None


class SessionQuotaSummary(BaseModel):
    status: str = "not_available"
    account_slug: str | None = None
    account_label: str | None = None
    model_id: str | None = None
    bucket_kind: Literal["standard", "spark", "unknown"] = "unknown"
    bucket_label: str | None = None
    windows: list[SessionQuotaWindowSummary] = Field(default_factory=list)
    selected_limit_id: str | None = None
    selected_limit_name: str | None = None
    selected_window_type: str | None = None
    selected_window_label: str | None = None
    remaining_percent: float | None = None
    resets_at: str | int | None = None
    unavailable_reason: str | None = None
    five_hour_used_percent: float | None = None
    weekly_used_percent: float | None = None
    five_hour_reset_at: str | None = None
    weekly_reset_at: str | None = None


class ObservedProjectSession(BaseModel):
    chat_id: int | None = None
    thread_id: int
    composite_key: str | None = None
    topic_name: str
    project_path: str | None = None
    project_id: str | None = None
    inventory_identity_kind: str | None = None
    topic_config_key: str | None = Field(default=None, exclude=True)
    archive_available: bool = False
    archive_unavailable_reason: str | None = None
    tmux_session_name: str | None = None
    tmux_session_running: bool = False
    agent: str
    runtime_selection_status: Literal["current", "unknown", "not_running", "unsupported"] = (
        "unsupported"
    )
    current_model: str | None = None
    current_reasoning_effort: str | None = None
    account_slug: str | None = None
    account_label: str | None = None
    account_profile_path: str | None = None
    account_source: str = Field(description="runtime_control | process_env | state | unknown")
    quota: SessionQuotaSummary = Field(default_factory=SessionQuotaSummary)
    topic_status: str = "unknown"
    session_status: str = "unknown"
    management_status: str = "not_configured"
    activity_status: str = "unknown"
    archived: bool = False
    archive_revision: int | None = None
    inventory_reason: str | None = None
    path_exists: bool | None = None
    session_running: bool | None = None
    queue_processing: bool | None = None
    agent_processing: bool | None = None
    is_processing: bool | None = None
    queue_busy: bool | None = None
    pending_count: int | None = None
    task_started_at: str | None = None
    last_task_completed_at: str | None = None
    last_task_outcome: str | None = None
    observed_at: str | None = None
    onboarding_state: Literal["UNCONFIGURED", "CONFIGURED"] = "UNCONFIGURED"
    missing_launch_fields: list[str] = Field(default_factory=list)
    setup_action_enabled: bool = False


class RuntimeIntegrationStatus(BaseModel):
    status: str = Field(description="compatible | unavailable")
    bot_root: str
    switch_actions_allowed: bool = False
    reason: str | None = None


class ObservedProjectSessionsResponse(BaseModel):
    integration: RuntimeIntegrationStatus
    project_count: int
    projects: list[ObservedProjectSession]
    warnings: list[str] = Field(default_factory=list)


class ProjectInventoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_kind: Literal["project_path", "telegram_topic", "topic_config_entry"] = "project_path"
    project_path: str | None = None
    archived: bool
    expected_revision: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=240)


class ProjectInventoryUpdateResponse(BaseModel):
    project_id: str
    identity_kind: str
    project_path: str | None = None
    archived: bool
    revision: int
    reason: str | None = None
    updated_at: str
