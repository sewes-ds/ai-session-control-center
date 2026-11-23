from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderSelectionValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: str | None = None
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str


class ProviderSelectionValidationResponse(BaseModel):
    status: Literal["valid", "invalid"]
    provider_id: str
    account_profile: str
    model: str
    reasoning_effort: str
    capability_source: Literal["current", "last_known_good", "missing"]
    capabilities_discovered_at: str | None = None
    model_display_name: str | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
