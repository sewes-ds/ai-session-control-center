from pydantic import BaseModel, Field

from ai_account_manager.schemas.codex import CodexCapabilitiesSnapshot


class ClaudeCodeAccountProfile(BaseModel):
    provider_id: str = "claude_code"
    account_slug: str
    label: str
    backend: str = "deepseek"
    profile_path: str
    profile_path_exists: bool
    has_api_key: bool
    auth_ready: bool
    status: str = Field(description="readable | missing_key | invalid_key_file | invalid_key")


class ClaudeCodeProfileDiscoveryResponse(BaseModel):
    provider_id: str = "claude_code"
    account_count: int
    accounts: list[ClaudeCodeAccountProfile]


class DeepSeekBalanceItem(BaseModel):
    currency: str
    total_balance: str
    granted_balance: str
    topped_up_balance: str


class DeepSeekBalanceSnapshot(BaseModel):
    version: int = 1
    provider_id: str = "claude_code"
    backend: str = "deepseek"
    account_slug: str
    status: str = Field(description="ok | not_refreshed | error")
    fetched_at: str | None = None
    source_endpoint: str | None = None
    is_available: bool | None = None
    balance_infos: list[DeepSeekBalanceItem] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


ClaudeCodeCapabilitiesSnapshot = CodexCapabilitiesSnapshot
