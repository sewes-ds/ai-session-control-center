from pydantic import BaseModel

from ai_account_manager.core.config import AppSettings


class RuntimePublicConfig(BaseModel):
    app_env: str
    app_host: str
    app_port: int
    ai_profiles_root: str
    ai_project_roots: list[str]
    ai_quota_cache_root: str
    runtime_data_root: str
    telegram_ai_agent_root: str
    enabled_providers: dict[str, bool]

    @classmethod
    def from_settings(cls, settings: AppSettings) -> "RuntimePublicConfig":
        return cls(**settings.to_public_dict())
