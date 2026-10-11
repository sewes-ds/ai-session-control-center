from fastapi import APIRouter, HTTPException, Request
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.auth.telegram_topic_access import (
    TelegramTopicAccessError,
    verify_forum_member_access,
)
from ai_account_manager.auth.telegram import TelegramInitData
from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.project_onboarding import (
    ProjectOnboardingError,
    validate_onboarding_project_path,
)
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    project_id_for_path,
    validate_provider_project_settings,
)
from ai_account_manager.core.runtime_control import read_runtime_control_store
from ai_account_manager.core.runtime_onboarding import (
    RuntimeOnboardingError,
    finalize_topic_onboarding,
    find_recoverable_topic_onboarding_token,
    mark_setup_button_refreshed,
    mark_onboarding_completion_notified,
    prepare_topic_onboarding_binding,
    renew_expired_topic_onboarding_token,
    resolve_topic_onboarding_token,
)
from ai_account_manager.integrations.ai_agent_runtime import (
    RuntimeConnector,
    RuntimeConnectorError,
)
from ai_account_manager.integrations.cus_bot import (
    CusBotDeliveryError,
    replace_setup_button,
    send_topic_notification,
)
from ai_account_manager.schemas.runtime_control import OnboardingRecord, RuntimeBindingRecord

router = APIRouter(tags=["onboarding"])


class OnboardingApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_id: str = Field(pattern=r"^onb_[0-9a-f]{32}$")
    project_path: str = Field(min_length=1, max_length=4096)
    provider: str = Field(min_length=1, max_length=64)
    account_profile: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=100)


async def _authorized_record(
    token: str,
    request: Request,
) -> tuple[AppSettings, TelegramInitData, OnboardingRecord]:
    if getattr(request.state, "telegram_user", None) is None:
        raise HTTPException(status_code=401, detail="Telegram authorization is required")
    settings = get_settings()
    expired = False
    try:
        record = resolve_topic_onboarding_token(settings.runtime_control_path, token)
    except RuntimeOnboardingError as exc:
        if exc.code != "ONBOARDING_TOKEN_EXPIRED":
            try:
                record = find_recoverable_topic_onboarding_token(
                    settings.runtime_control_path, token
                )
            except RuntimeOnboardingError:
                raise HTTPException(
                    status_code=404,
                    detail={"code": exc.code, "message": exc.message},
                ) from exc
        else:
            try:
                record = find_recoverable_topic_onboarding_token(
                    settings.runtime_control_path, token
                )
            except RuntimeOnboardingError as lookup_exc:
                raise HTTPException(
                    status_code=410,
                    detail={"code": lookup_exc.code, "message": lookup_exc.message},
                ) from lookup_exc
        expired = True
    principal = request.state.telegram_user
    if not isinstance(principal, TelegramInitData):
        raise HTTPException(status_code=401, detail="Telegram authorization is required")
    try:
        await verify_forum_member_access(
            bot_token=settings.cus_telegram_bot_token,
            chat_id=record.chat_id,
            user_id=principal.user_id,
        )
    except TelegramTopicAccessError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 403,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if expired:
        if request.method != "GET":
            raise HTTPException(
                status_code=410,
                detail={"code": "ONBOARDING_TOKEN_EXPIRED", "message": "Reopen the setup link"},
            )
        try:
            issued = renew_expired_topic_onboarding_token(
                settings.runtime_control_path,
                old_token=token,
                token_key=settings.cus_onboarding_token_key,
                ttl_seconds=settings.cus_onboarding_token_ttl_seconds,
            )
            await replace_setup_button(
                bot_token=settings.cus_telegram_bot_token,
                chat_id=issued.record.chat_id,
                message_id=issued.record.setup_message_id,
                deep_link=f"{settings.cus_miniapp_deep_link_base}{quote(issued.token, safe='')}",
                timeout_seconds=settings.runtime_connector_timeout_seconds,
            )
            mark_setup_button_refreshed(
                settings.runtime_control_path,
                onboarding_id=issued.record.onboarding_id,
                token=issued.token,
                message_id=issued.record.setup_message_id,
            )
        except RuntimeOnboardingError as exc:
            raise HTTPException(
                status_code=409, detail={"code": exc.code, "message": exc.message}
            ) from exc
        except CusBotDeliveryError as exc:
            raise HTTPException(
                status_code=503 if exc.retryable else 409,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        raise HTTPException(
            status_code=410,
            detail={"code": "ONBOARDING_TOKEN_EXPIRED", "message": "Setup button has been renewed"},
        )
    return settings, principal, record


@router.get("/onboarding/{token}")
async def resolve_onboarding(token: str, request: Request) -> dict[str, object]:
    # A deep-link token selects a topic but never authenticates the user. Preview
    # access is intentionally insufficient for this endpoint.
    _settings, _principal, record = await _authorized_record(token, request)
    return {
        "onboarding_id": record.onboarding_id,
        "agent_instance_id": record.agent_instance_id,
        "chat_id": record.chat_id,
        "thread_id": record.thread_id,
        "topic_name": record.topic_name,
        "state": record.state,
        "completion_notified": record.completion_message_id is not None,
        "expires_at": record.expires_at,
    }


@router.post("/onboarding/{token}/complete")
async def complete_onboarding(token: str, request: Request) -> dict[str, object]:
    settings, _principal, record = await _authorized_record(token, request)
    if record.state != "CONFIGURED" or record.binding_id is None:
        raise HTTPException(status_code=409, detail="Onboarding is not configured")
    binding = read_runtime_control_store(settings.runtime_control_path).bindings.get(
        record.binding_id
    )
    if binding is None or binding.lifecycle_status != "CONFIGURED":
        raise HTTPException(status_code=409, detail="Configured binding is missing")
    notified = await _ensure_completion_notification(settings, record, binding)
    return _configured_response(notified, binding)


@router.post("/onboarding/{token}/apply")
async def apply_onboarding(
    token: str,
    payload: OnboardingApplyRequest,
    request: Request,
) -> dict[str, object]:
    settings, principal, record = await _authorized_record(token, request)
    if record.onboarding_id != payload.onboarding_id:
        raise HTTPException(status_code=409, detail="Onboarding identity mismatch")
    if record.state == "CONFIGURED" and record.binding_id is not None:
        binding = read_runtime_control_store(settings.runtime_control_path).bindings.get(
            record.binding_id
        )
        if binding is None or binding.lifecycle_status != "CONFIGURED":
            raise HTTPException(status_code=409, detail="Configured binding is missing")
        notified = await _ensure_completion_notification(settings, record, binding)
        return _configured_response(notified, binding)
    try:
        canonical_cwd = validate_onboarding_project_path(payload.project_path, settings)
        validate_provider_project_settings(
            provider_id=payload.provider,
            account_profile=payload.account_profile,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            settings=settings,
        )
        prepared = prepare_topic_onboarding_binding(
            settings.runtime_control_path,
            onboarding_id=payload.onboarding_id,
            token=token,
            project_id=project_id_for_path(canonical_cwd),
            canonical_cwd=canonical_cwd,
            provider=payload.provider,
            account_profile=payload.account_profile,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            source_ref=f"telegram-user:{principal.user_id}",
        )
    except (ProjectOnboardingError, ProjectSettingsError, RuntimeOnboardingError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    connector = RuntimeConnector(
        prepared.runtime_socket,
        timeout=settings.runtime_connector_timeout_seconds,
    )
    try:
        await connector.apply_binding(
            prepared.binding,
            request_id=(
                f"onboarding-apply:{prepared.record.onboarding_id}:"
                f"{prepared.binding.desired.spec_digest[7:]}"
            ),
        )
    except RuntimeConnectorError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    configured_record, configured_binding = finalize_topic_onboarding(
        settings.runtime_control_path,
        onboarding_id=prepared.record.onboarding_id,
        binding_id=prepared.binding.binding_id,
        expected_spec_digest=prepared.binding.desired.spec_digest,
    )
    notified = await _ensure_completion_notification(
        settings,
        configured_record,
        configured_binding,
    )
    return _configured_response(notified, configured_binding)


async def _ensure_completion_notification(
    settings: AppSettings,
    record: OnboardingRecord,
    binding: RuntimeBindingRecord,
) -> OnboardingRecord:
    if record.completion_message_id is not None:
        return record
    try:
        message_id = await send_topic_notification(
            bot_token=settings.cus_telegram_bot_token,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            text="Настройка завершена. Можно начинать работу",
            timeout_seconds=settings.runtime_connector_timeout_seconds,
        )
    except CusBotDeliveryError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return mark_onboarding_completion_notified(
        settings.runtime_control_path,
        onboarding_id=record.onboarding_id,
        binding_id=binding.binding_id,
        message_id=message_id,
    )


def _configured_response(
    record: OnboardingRecord,
    binding: RuntimeBindingRecord,
) -> dict[str, object]:
    return {
        "status": "CONFIGURED",
        "close_miniapp": True,
        "onboarding_id": record.onboarding_id,
        "binding_id": binding.binding_id,
        "generation_id": binding.desired.generation_id,
        "revision": binding.desired.revision,
        "spec_digest": binding.desired.spec_digest,
    }
