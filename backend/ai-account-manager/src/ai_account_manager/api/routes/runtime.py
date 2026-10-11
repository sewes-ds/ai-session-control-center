from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.auth.telegram import TelegramInitData
from ai_account_manager.auth.telegram_topic_access import (
    TelegramTopicAccessError,
    verify_forum_member_access,
)
from ai_account_manager.core.config import get_settings
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    validate_provider_project_settings,
)
from ai_account_manager.core.runtime_control import (
    RuntimeControlStoreError,
    commit_binding_selection,
    read_runtime_control_store,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.integrations.runtime_health import collect_runtime_binding_health
from ai_account_manager.schemas.runtime import RuntimePublicConfig
from ai_account_manager.schemas.runtime_health import RuntimeBindingHealthResponse

router = APIRouter(tags=["runtime"])


class BindingSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    expected_spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    account_profile: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)


@router.get("/runtime/config-public", response_model=RuntimePublicConfig)
def runtime_public_config() -> RuntimePublicConfig:
    settings = get_settings()
    return RuntimePublicConfig.from_settings(settings)


@router.get("/runtime/bindings/health", response_model=RuntimeBindingHealthResponse)
async def runtime_bindings_health() -> RuntimeBindingHealthResponse:
    settings = get_settings()
    return await collect_runtime_binding_health(
        store_path=settings.runtime_control_path,
        timeout=settings.runtime_connector_timeout_seconds,
    )


@router.post("/runtime/bindings/{binding_id}/selection")
async def select_binding_profile(
    binding_id: str,
    payload: BindingSelectionRequest,
    request: Request,
) -> dict[str, object]:
    """Change desired settings for one topic; Runtime applies after IDLE."""

    principal = getattr(request.state, "telegram_user", None)
    if not isinstance(principal, TelegramInitData):
        raise HTTPException(status_code=401, detail="Telegram authorization is required")
    settings = get_settings()
    store = read_runtime_control_store(settings.runtime_control_path)
    binding = store.bindings.get(binding_id)
    if binding is None or binding.lifecycle_status != "CONFIGURED":
        raise HTTPException(status_code=404, detail="Configured binding not found")
    try:
        await verify_forum_member_access(
            bot_token=settings.cus_telegram_bot_token,
            chat_id=binding.chat_id,
            user_id=principal.user_id,
        )
    except TelegramTopicAccessError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    try:
        validation = validate_provider_project_settings(
            provider_id=binding.desired.provider,
            account_profile=payload.account_profile,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            settings=settings,
        )
        if validation.status != "valid" or validation.capability_source != "current":
            raise HTTPException(
                status_code=409,
                detail={"code": "capabilities_current_required", "message": "Refresh account models before saving"},
            )
        result, committed = commit_binding_selection(
            settings.runtime_control_path,
            binding_id=binding_id,
            expected_generation_id=payload.expected_generation_id,
            expected_revision=payload.expected_revision,
            expected_spec_digest=payload.expected_spec_digest,
            account_profile=payload.account_profile,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            source_ref=f"telegram-user:{principal.user_id}",
        )
    except (ProjectSettingsError, RuntimeControlStoreError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if result == "UNCHANGED":
        return {"status": "unchanged", "desired": committed.desired.runtime_payload()}
    agent = store.agent_instances[binding.agent_instance_id]
    try:
        await RuntimeConnector(
            Path(agent.runtime_socket),
            timeout=settings.runtime_connector_timeout_seconds,
        ).apply_binding(
            committed,
            request_id=f"desired-apply:{binding_id}:{committed.desired.spec_digest[7:]}",
        )
    except RuntimeConnectorError as exc:
        return {
            "status": "pending_runtime",
            "desired": committed.desired.runtime_payload(),
            "diagnostic_code": exc.code,
        }
    return {"status": "pending_idle_or_launch", "desired": committed.desired.runtime_payload()}
