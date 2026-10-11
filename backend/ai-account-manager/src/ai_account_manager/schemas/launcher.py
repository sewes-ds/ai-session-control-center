from pydantic import BaseModel, ConfigDict, Field


class LauncherResolveRequest(BaseModel):
    provider_id: str = Field(description="Provider id, for example codex")
    account_slug: str = Field(description="Provider account/profile slug")
    project_path: str = Field(description="Absolute server-side project directory path")


class LauncherResolveResponse(BaseModel):
    provider_id: str
    account_slug: str
    project_path: str
    cwd: str
    command: str
    argv: list[str]
    env: dict[str, str]
    profile_path: str
    profile_path_exists: bool
    auth_status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ProjectSettingsResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    expected_settings_revision: int = Field(ge=1)
    allow_last_known_good: bool = False


class ImmutableLaunchSpec(BaseModel):
    spec_version: int = 1
    provider_id: str
    project_id: str
    project_path: str
    cwd: str
    account_profile: str
    model: str
    reasoning_effort: str
    settings_revision: int
    command: str
    argv: list[str]
    env: dict[str, str]
    auth_status: str
    capability_source: str
    warnings: list[str] = Field(default_factory=list)
    spec_sha256: str
