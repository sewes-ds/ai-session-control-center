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
    runtime_selection_status: Literal[
        "current", "unknown", "not_running", "unsupported"
    ] = "unsupported"
    current_model: str | None = None
    current_reasoning_effort: str | None = None
    account_slug: str | None = None
    account_label: str | None = None
    account_profile_path: str | None = None
    account_source: str = Field(
        description="process_env | state | binding | transcript | default_profile | unknown"
    )
    quota: SessionQuotaSummary = Field(default_factory=SessionQuotaSummary)
    topic_status: str = Field(default="unknown", description="configured | closed | archived | unknown")
    session_status: str = Field(default="unknown", description="running | stopped | stale_state | orphan_tmux | unknown")
    management_status: str = Field(default="not_configured", description="managed | legacy_unmanaged | not_configured | runtime_invalid")
    activity_status: str = Field(default="unknown", description="working | queued | ready | session_stopped | unknown")
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
    onboarding_state: Literal["LEGACY", "UNCONFIGURED", "CONFIGURED"] = "LEGACY"
    missing_launch_fields: list[str] = Field(default_factory=list)
    setup_action_enabled: bool = False


class RuntimeIntegrationStatus(BaseModel):
    status: str = Field(description="compatible | incompatible | unavailable | invalid_manifest")
    bot_root: str
    bot_commit: str | None = None
    supported_commit: str | None = None
    target_files_match: bool = False
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


class ProjectBinding(BaseModel):
    project_path: str
    provider_id: str
    account_slug: str
    profile_path: str
    updated_at: str


class ProjectBindingsResponse(BaseModel):
    binding_count: int
    bindings: list[ProjectBinding]


class ProjectBindingUpsertRequest(BaseModel):
    project_path: str
    provider_id: str
    account_slug: str


class ProjectAccountSwitchRequest(BaseModel):
    project_path: str
    account_slug: str
    confirm_interrupt: bool = False


class ProjectAccountSwitchResponse(BaseModel):
    status: str = Field(description="switched | no_change")
    project_path: str
    topic_name: str
    thread_id: int
    tmux_session_name: str
    previous_account_slug: str
    account_slug: str
    session_restarted: bool
    rollback_performed: bool = False


ProviderSettingsStatus = Literal["valid", "invalid"]
CapabilitySource = Literal["current", "last_known_good", "missing"]
RuntimeApplicationStatus = Literal[
    "incomplete",
    "account_matches_settings_pending",
    "account_differs",
    "runtime_unknown",
    "applied",
    "runtime_spec_stale",
    "runtime_spec_invalid",
]
MigrationStatus = Literal["migrated", "already_migrated", "partial", "empty_created"]


class ProjectSettingsRecord(BaseModel):
    project_id: str
    project_path: str
    provider_id: str
    account_profile: str
    model: str | None = None
    reasoning_effort: str | None = None
    updated_at: str
    revision: int = Field(ge=1)
    source: Literal["legacy_v1", "api"]


class ProjectSettingsRuntimeView(BaseModel):
    runtime_account_profile: str | None = None
    runtime_account_matches: bool | None = None
    runtime_application_status: RuntimeApplicationStatus
    runtime_model: str | None = None
    runtime_reasoning_effort: str | None = None
    runtime_settings_revision: int | None = None
    runtime_spec_sha256: str | None = None


class ProjectSettingsProjectView(ProjectSettingsRecord, ProjectSettingsRuntimeView):
    pass


class ProjectSettingsMigrationMetadata(BaseModel):
    source: Literal["project_bindings_v1"] = "project_bindings_v1"
    source_sha256: str | None = None
    migrated_at: str | None = None


class ProjectSettingsListResponse(BaseModel):
    version: int = 2
    store_revision: int
    project_count: int
    projects: list[ProjectSettingsProjectView]
    migrated_from: ProjectSettingsMigrationMetadata
    migration_required: bool


class ProjectSettingsGetResponse(ProjectSettingsProjectView):
    pass


class ProjectSettingsValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str


class ProjectSettingsValidationResponse(BaseModel):
    status: ProviderSettingsStatus
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str
    capability_source: CapabilitySource
    capabilities_discovered_at: str | None = None
    model_display_name: str | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ProjectSettingsApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    expected_settings_revision: int = Field(ge=1)
    confirm_interrupt: bool = False
    allow_last_known_good: bool = False


ProjectSettingsApplyStatus = Literal["applied", "no_change"]
ProjectSettingsSessionAction = Literal["started", "restarted", "unchanged"]


class ProjectSettingsApplyResponse(BaseModel):
    status: ProjectSettingsApplyStatus
    project_id: str
    project_path: str
    topic_name: str
    thread_id: int
    tmux_session_name: str
    settings_revision: int
    runtime_revision: int
    account_profile: str
    model: str
    reasoning_effort: str
    spec_sha256: str
    session_action: ProjectSettingsSessionAction = "unchanged"
    session_started: bool = False
    session_restarted: bool
    rollback_performed: bool
    previous_account_profile: str | None = None
    previous_model: str | None = None
    previous_reasoning_effort: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ProjectSettingsSelectionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str
    expected_settings_revision: int | None = Field(default=None, ge=1)
    confirm_interrupt: bool = False
    allow_last_known_good: bool = False


class ProjectOnboardingPathBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    project_path: str


class ProjectOnboardingPathBindResponse(BaseModel):
    status: Literal["bound"] = "bound"
    previous_project_id: str
    project_id: str
    chat_id: int
    thread_id: int
    project_path: str
    onboarding_state: Literal["UNCONFIGURED"] = "UNCONFIGURED"
    missing_launch_fields: list[str] = Field(default_factory=list)


class ProjectOnboardingApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str
    expected_settings_revision: int | None = Field(default=None, ge=1)
    confirm: bool = False


class ProjectOnboardingApplyResponse(BaseModel):
    status: Literal["configured"] = "configured"
    project_id: str
    project_path: str
    chat_id: int
    thread_id: int
    account_profile: str
    model: str
    reasoning_effort: str
    settings_revision: int
    runtime_revision: int
    spec_sha256: str
    onboarding_state: Literal["CONFIGURED"] = "CONFIGURED"
    session_started: bool = False
    next_action: str = "SEND_FIRST_TELEGRAM_MESSAGE"


class ProjectSettingsUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str
    expected_revision: int | None = Field(default=None, ge=1)


class ProjectSettingsUpsertResponse(ProjectSettingsProjectView):
    persisted: bool = True
    applied_to_runtime: bool = False


class ProjectSettingsMigrationResult(BaseModel):
    status: MigrationStatus
    imported_count: int
    skipped_count: int = 0
    skipped: list[dict[str, str]] = Field(default_factory=list)
    store_revision: int
    source_sha256: str | None = None
    settings_path: str
