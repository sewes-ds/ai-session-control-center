from pydantic import BaseModel, ConfigDict, Field


class CodexAccountProfile(BaseModel):
    account_slug: str = Field(description="Directory name under the Codex profiles root")
    label: str = Field(description="Display label derived from profile metadata or slug")
    profile_path: str = Field(description="Server-side profile directory path")
    profile_path_exists: bool
    has_auth_json: bool
    has_config_toml: bool
    has_profile_json: bool
    status: str = Field(description="readable | missing_auth | missing_config | invalid_slug")


class CodexProfileDiscoveryResponse(BaseModel):
    provider_id: str = "codex"
    profiles_root: str
    codex_profiles_root: str
    root_exists: bool
    account_count: int
    accounts: list[CodexAccountProfile]


class CodexQuotaWindow(BaseModel):
    window_id: str = Field(description="primary | secondary")
    window_type: str | None = Field(default=None, description="primary | secondary")
    used_percent: float | None = None
    remaining_percent: float | None = None
    window_duration_minutes: int | None = None
    resets_at: str | int | None = None
    limit_window_seconds: int | None = None
    reset_after_seconds: int | None = None
    reset_at: str | None = None


class CodexQuotaResetCreditItem(BaseModel):
    credit_id: str | None = None
    title: str | None = None
    expires_at: str | int | None = None


class CodexQuotaResetCredits(BaseModel):
    available_count: int | None = None
    credits: list[CodexQuotaResetCreditItem] | None = None


class CodexQuotaBucket(BaseModel):
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    rate_limit_reached_type: str | None = None
    windows: list[CodexQuotaWindow] = Field(default_factory=list)


class CodexQuotaSnapshot(BaseModel):
    provider_id: str = "codex"
    account_slug: str
    status: str = Field(
        description="ok | stale | missing | error | account_unregistered | account_unknown | not_refreshed"
    )
    fetched_at: str | None = None
    source_endpoint: str | None = None
    source_method: str | None = None
    buckets: list[CodexQuotaBucket] = Field(default_factory=list)
    reset_credits: CodexQuotaResetCredits = Field(default_factory=CodexQuotaResetCredits)
    windows: list[CodexQuotaWindow] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class CodexRateLimitResetCreditConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    credit_id: str | None = Field(default=None, max_length=512)


class CodexRateLimitResetCreditConsumeResponse(BaseModel):
    provider_id: str = "codex"
    account_slug: str
    outcome: str = Field(description="reset | alreadyRedeemed | nothingToReset | noCredit")
    success: bool
    quota: CodexQuotaSnapshot | None = None
    error_code: str | None = None
    error_message: str | None = None


class CodexCapabilityErrorFields(BaseModel):
    error_code: str | None = None
    error_message: str | None = None


class CodexCapabilityAccountSection(CodexCapabilityErrorFields):
    status: str = Field(description="ok | error | not_refreshed")
    authorized: bool | None = None
    account_type: str | None = None
    plan_type: str | None = None
    safe_identifier: str | None = None
    email_masked: str | None = None
    discovered_at: str | None = None


class CodexCapabilityModelItem(BaseModel):
    model_id: str
    display_name: str | None = None
    hidden: bool | None = None
    is_default: bool | None = None
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)
    supports_personality: bool | None = None


class CodexCapabilityModelsSection(CodexCapabilityErrorFields):
    status: str = Field(description="ok | error | not_refreshed")
    discovered_at: str | None = None
    items: list[CodexCapabilityModelItem] = Field(default_factory=list)
    next_cursor: str | None = None


class CodexCapabilityRateLimitWindow(BaseModel):
    window_type: str
    used_percent: float | None = None
    remaining_percent: float | None = None
    window_duration_minutes: int | None = None
    resets_at: int | str | None = None


class CodexCapabilityResetCreditItem(BaseModel):
    credit_id_sha256_12: str | None = None
    reset_type: str | None = None
    status: str | None = None
    granted_at: int | str | None = None
    expires_at: int | str | None = None
    title: str | None = None
    description: str | None = None


class CodexCapabilityResetCredits(BaseModel):
    available_count: int | None = None
    items: list[CodexCapabilityResetCreditItem] = Field(default_factory=list)


class CodexCapabilityRateLimitBucket(BaseModel):
    limit_id: str | None = None
    limit_name: str | None = None
    plan_type: str | None = None
    rate_limit_reached_type: str | None = None
    windows: list[CodexCapabilityRateLimitWindow] = Field(default_factory=list)


class CodexCapabilityRateLimitsSection(CodexCapabilityErrorFields):
    status: str = Field(description="ok | error | not_refreshed")
    discovered_at: str | None = None
    buckets: list[CodexCapabilityRateLimitBucket] = Field(default_factory=list)
    credits: dict[str, object] | None = None
    reset_credits: CodexCapabilityResetCredits = Field(default_factory=CodexCapabilityResetCredits)


class CodexCapabilityUsageSection(CodexCapabilityErrorFields):
    status: str = Field(description="ok | error | not_refreshed")
    discovered_at: str | None = None
    summary: dict[str, object] | None = None
    daily_usage_buckets: list[dict[str, object]] = Field(default_factory=list)


class CodexCapabilitiesSnapshot(BaseModel):
    version: int = 1
    provider_id: str = "codex"
    account_profile: str
    profile_path: str
    source: str = "codex_app_server"
    status: str = Field(description="ok | partial | error | not_refreshed")
    refreshed_at: str | None = None
    last_success_at: str | None = None
    profile_integrity_status: str = Field(description="unchanged | changed | not_checked")
    account: CodexCapabilityAccountSection
    models: CodexCapabilityModelsSection
    rate_limits: CodexCapabilityRateLimitsSection
    usage: CodexCapabilityUsageSection
    error_code: str | None = None
    error_message: str | None = None


class CodexAuthStatus(BaseModel):
    provider_id: str = "codex"
    account_slug: str
    profile_path: str
    profile_path_exists: bool
    has_auth_json: bool
    auth_json_mtime: str | None = None
    auth_mode: str | None = None
    has_tokens_object: bool = False
    has_access_token: bool = False
    has_refresh_token: bool = False
    has_account_id: bool = False
    last_refresh: str | None = None
    last_refresh_age_seconds: int | None = None
    stale_after_seconds: int | None = None
    is_stale_by_last_refresh: bool = False
    status: str = Field(
        description="ready | profile_missing | auth_missing | auth_invalid | tokens_missing | access_missing | stale_by_last_refresh"
    )
    recommendation: str = Field(
        description="Safe operator-facing next step. Never contains token values."
    )


class CodexAccountEnrollmentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_slug: str = Field(description="New Codex account/profile slug")
    label: str | None = Field(
        default=None,
        description="Optional display label. No password or secret fields are accepted.",
        max_length=120,
    )


class CodexAccountEnrollment(BaseModel):
    enrollment_id: str
    provider_id: str = "codex"
    account_slug: str
    label: str
    status: str = Field(
        description="starting | waiting_for_user | completed | failed | cancelled | expired"
    )
    created_at: str
    updated_at: str
    expires_at: str
    verification_url: str | None = None
    user_code: str | None = None
    profile_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
