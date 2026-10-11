import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core import runtime_launch_specs
from ai_account_manager.core.launcher import LauncherResolveError, build_project_launch_spec
from ai_account_manager.core.project_settings import (
    ProjectSettingsError,
    canonical_project_path,
    read_project_settings_store,
    runtime_view_for_record,
    upsert_project_settings,
    validate_project_id,
    validate_provider_project_settings,
)
from ai_account_manager.core.project_inventory import (
    ProjectInventoryError,
    update_config_topic_inventory_record,
    update_project_inventory_record,
    update_topic_inventory_record,
)
from ai_account_manager.integrations.session_runtime import (
    SessionRuntimeError,
    apply_project_onboarding_selection,
    apply_project_codex_selection,
    apply_project_codex_settings,
    bind_project_onboarding_path,
    list_project_bindings,
    observe_project_sessions,
    refresh_project_runtime_selections,
    switch_project_codex_account,
    upsert_project_binding,
)
from ai_account_manager.integrations.runtime_selection import RuntimeSelectionRefreshBusy
from ai_account_manager.schemas.sessions import (
    ObservedProjectSessionsResponse,
    ProjectAccountSwitchRequest,
    ProjectAccountSwitchResponse,
    ProjectBinding,
    ProjectBindingsResponse,
    ProjectBindingUpsertRequest,
    ProjectSettingsApplyRequest,
    ProjectSettingsApplyResponse,
    ProjectSettingsGetResponse,
    ProjectSettingsListResponse,
    ProjectSettingsMigrationMetadata,
    ProjectSettingsProjectView,
    ProjectSettingsRecord,
    ProjectSettingsSelectionApplyRequest,
    ProjectSettingsUpsertRequest,
    ProjectSettingsUpsertResponse,
    ProjectSettingsValidationRequest,
    ProjectSettingsValidationResponse,
    ProjectInventoryUpdateRequest,
    ProjectInventoryUpdateResponse,
    ProjectOnboardingApplyRequest,
    ProjectOnboardingApplyResponse,
    ProjectOnboardingPathBindRequest,
    ProjectOnboardingPathBindResponse,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _settings_http_error(exc: ProjectSettingsError) -> HTTPException:
    code_to_status = {
        "invalid_project_id": status.HTTP_400_BAD_REQUEST,
        "project_path_not_absolute": status.HTTP_400_BAD_REQUEST,
        "project_outside_allowed_roots": status.HTTP_400_BAD_REQUEST,
        "invalid_provider_id": status.HTTP_400_BAD_REQUEST,
        "invalid_account_profile": status.HTTP_400_BAD_REQUEST,
        "invalid_model": status.HTTP_400_BAD_REQUEST,
        "invalid_reasoning_effort": status.HTTP_400_BAD_REQUEST,
        "project_missing": status.HTTP_404_NOT_FOUND,
        "profile_missing": status.HTTP_404_NOT_FOUND,
        "settings_not_found": status.HTTP_404_NOT_FOUND,
        "capabilities_refresh_required": status.HTTP_409_CONFLICT,
        "model_not_available": status.HTTP_409_CONFLICT,
        "reasoning_not_supported": status.HTTP_409_CONFLICT,
        "provider_settings_not_supported": status.HTTP_409_CONFLICT,
        "revision_conflict": status.HTTP_409_CONFLICT,
        "settings_store_invalid": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "settings_write_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    return HTTPException(
        status_code=code_to_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": exc.message},
    )


def _inventory_http_error(exc: ProjectInventoryError) -> HTTPException:
    mapping = {
        "invalid_project_id": status.HTTP_400_BAD_REQUEST,
        "project_id_mismatch": status.HTTP_400_BAD_REQUEST,
        "project_path_not_absolute": status.HTTP_400_BAD_REQUEST,
        "project_outside_allowed_roots": status.HTTP_400_BAD_REQUEST,
        "invalid_inventory_path": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "inventory_store_invalid": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "revision_conflict": status.HTTP_409_CONFLICT,
        "archive_running_blocked": status.HTTP_409_CONFLICT,
        "archive_identity_unavailable": status.HTTP_409_CONFLICT,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": exc.message},
    )


def _runtime_http_error(exc: SessionRuntimeError) -> HTTPException:
    bounded_topic_mismatch_codes = {
        "topic_not_found",
        "topic_type_mismatch",
        "topic_path_mismatch",
        "topic_exec_mode_mismatch",
        "topic_engine_mismatch",
        "session_blueprint_path_mismatch",
        "session_blueprint_provider_mismatch",
        "session_blueprint_mode_mismatch",
        "session_state_mismatch",
    }
    code_to_status = {
        "invalid_project_id": status.HTTP_400_BAD_REQUEST,
        "invalid_request": status.HTTP_400_BAD_REQUEST,
        "PATH_REQUIRED": status.HTTP_400_BAD_REQUEST,
        "PATH_INVALID": status.HTTP_400_BAD_REQUEST,
        "PATH_NOT_ALLOWED": status.HTTP_403_FORBIDDEN,
        "ACCOUNT_REQUIRED": status.HTTP_400_BAD_REQUEST,
        "MODEL_REQUIRED": status.HTTP_400_BAD_REQUEST,
        "RUNTIME_SPEC_INVALID": status.HTTP_409_CONFLICT,
        "PROJECT_IDENTITY_MISSING": status.HTTP_409_CONFLICT,
        "PROJECT_NOT_CONFIGURED": status.HTTP_409_CONFLICT,
        "settings_missing": status.HTTP_404_NOT_FOUND,
        "project_not_configured": status.HTTP_404_NOT_FOUND,
        "profile_missing": status.HTTP_404_NOT_FOUND,
        "confirmation_required": status.HTTP_409_CONFLICT,
        "revision_mismatch": status.HTTP_409_CONFLICT,
        "settings_incomplete": status.HTTP_409_CONFLICT,
        "capabilities_current_required": status.HTTP_409_CONFLICT,
        "capabilities_refresh_required": status.HTTP_409_CONFLICT,
        "model_not_available": status.HTTP_409_CONFLICT,
        "reasoning_not_supported": status.HTTP_409_CONFLICT,
        "provider_settings_not_supported": status.HTTP_409_CONFLICT,
        "integration_incompatible": status.HTTP_409_CONFLICT,
        "project_agent_not_codex": status.HTTP_409_CONFLICT,
        "session_not_running": status.HTTP_409_CONFLICT,
        "session_mismatch": status.HTTP_409_CONFLICT,
        "topic_mismatch": status.HTTP_409_CONFLICT,
        **{code: status.HTTP_409_CONFLICT for code in bounded_topic_mismatch_codes},
        "work_in_progress": status.HTTP_409_CONFLICT,
        "work_interrupt_failed": status.HTTP_409_CONFLICT,
        "settings_mismatch": status.HTTP_409_CONFLICT,
        "spec_sha256_mismatch": status.HTTP_409_CONFLICT,
        "revision_conflict": status.HTTP_409_CONFLICT,
        "runtime_apply_recovery_required": status.HTTP_409_CONFLICT,
        "runtime_process_verification_failed": status.HTTP_502_BAD_GATEWAY,
        "session_start_failed": status.HTTP_502_BAD_GATEWAY,
        "apply_failed_rolled_back": status.HTTP_502_BAD_GATEWAY,
        "post_apply_verification_failed": status.HTTP_502_BAD_GATEWAY,
        "post_apply_runtime_truth_unavailable": status.HTTP_502_BAD_GATEWAY,
        "post_apply_selection_mismatch": status.HTTP_502_BAD_GATEWAY,
        "apply_failed_rollback_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "settings_apply_rollback_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "runtime_launch_spec_store_invalid": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "settings_store_invalid": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "control_response_invalid": status.HTTP_500_INTERNAL_SERVER_ERROR,
        "control_socket_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        "control_socket_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    message = exc.message
    if exc.code in bounded_topic_mismatch_codes:
        message = (
            "Конфигурация проекта не совпадает с настройками Telegram-топика. "
            "Запуск не выполнен; требуется операторская проверка."
        )
    return HTTPException(
        status_code=code_to_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail={"code": exc.code, "message": message},
    )


def _read_runtime_store_once(settings: AppSettings) -> tuple[dict[str, Any], bool]:
    path = settings.runtime_launch_specs_path.expanduser().resolve(strict=False)
    if not path.exists():
        return runtime_launch_specs.empty_runtime_launch_spec_store(), False
    if path.is_symlink() or not path.is_file():
        return runtime_launch_specs.empty_runtime_launch_spec_store(), True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return runtime_launch_specs._validate_store(raw), False
    except Exception:
        return runtime_launch_specs.empty_runtime_launch_spec_store(), True


def _expected_spec_sha256(record: ProjectSettingsRecord, settings: AppSettings) -> str | None:
    if record.model is None or record.reasoning_effort is None:
        return None
    try:
        spec = build_project_launch_spec(
            project_id=record.project_id,
            expected_settings_revision=record.revision,
            allow_last_known_good=True,
            settings=settings,
        )
    except (LauncherResolveError, ProjectSettingsError):
        return None
    return spec.spec_sha256


def _project_view(
    record: ProjectSettingsRecord,
    *,
    settings: AppSettings,
    bindings: list[ProjectBinding],
    runtime_store: dict[str, Any],
    runtime_store_invalid: bool,
) -> ProjectSettingsProjectView:
    runtime_record = None
    if not runtime_store_invalid:
        projects = runtime_store.get("projects")
        if isinstance(projects, dict):
            raw = projects.get(record.project_id)
            runtime_record = raw if isinstance(raw, dict) else None
    runtime = runtime_view_for_record(
        record,
        bindings,
        runtime_record=runtime_record,
        expected_spec_sha256=_expected_spec_sha256(record, settings),
        runtime_store_invalid=runtime_store_invalid,
    )
    return ProjectSettingsProjectView(**record.model_dump(), **runtime.model_dump())


def _records_from_store(store: dict[str, object]) -> list[ProjectSettingsRecord]:
    projects = store.get("projects")
    if not isinstance(projects, dict):
        return []
    records = [ProjectSettingsRecord.model_validate(raw) for raw in projects.values()]
    return sorted(records, key=lambda item: item.project_path)


@router.get("/projects", response_model=ObservedProjectSessionsResponse)
def list_observed_projects() -> ObservedProjectSessionsResponse:
    return observe_project_sessions()


@router.post(
    "/runtime-selection/refresh",
    response_model=ObservedProjectSessionsResponse,
)
async def refresh_observed_runtime_selection() -> ObservedProjectSessionsResponse:
    try:
        return await refresh_project_runtime_selections()
    except RuntimeSelectionRefreshBusy as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "runtime_selection_refresh_busy",
                "message": "Runtime selection refresh is already in progress",
            },
        ) from exc


@router.get("/bindings", response_model=ProjectBindingsResponse)
def read_project_bindings() -> ProjectBindingsResponse:
    return list_project_bindings()


@router.post(
    "/project-onboarding/bind-path",
    response_model=ProjectOnboardingPathBindResponse,
)
def bind_onboarding_path(
    request: ProjectOnboardingPathBindRequest,
) -> ProjectOnboardingPathBindResponse:
    try:
        return bind_project_onboarding_path(
            project_id=request.project_id,
            project_path=request.project_path,
        )
    except SessionRuntimeError as exc:
        raise _runtime_http_error(exc) from exc


@router.post(
    "/project-onboarding/apply",
    response_model=ProjectOnboardingApplyResponse,
)
def apply_onboarding_selection(
    request: ProjectOnboardingApplyRequest,
) -> ProjectOnboardingApplyResponse:
    try:
        return apply_project_onboarding_selection(
            project_path=request.project_path,
            provider_id=request.provider_id,
            account_profile=request.account_profile,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            expected_settings_revision=request.expected_settings_revision,
            confirm=request.confirm,
        )
    except SessionRuntimeError as exc:
        raise _runtime_http_error(exc) from exc


@router.put("/project-inventory/{project_id}", response_model=ProjectInventoryUpdateResponse)
def update_project_inventory(
    project_id: str,
    request: ProjectInventoryUpdateRequest,
) -> ProjectInventoryUpdateResponse:
    try:
        observed = observe_project_sessions()
        matching = next(
            (
                item
                for item in observed.projects
                if item.project_id == project_id
                and item.inventory_identity_kind == request.identity_kind
            ),
            None,
        )
        if request.archived:
            if request.identity_kind == "project_path":
                matching_path = (
                    str(Path(request.project_path).expanduser().resolve(strict=False))
                    if request.project_path is not None
                    else None
                )
                if matching is None or matching.project_path != matching_path:
                    raise ProjectInventoryError(
                        "archive_identity_unavailable",
                        "Project archive identity is not currently available",
                    )
            elif matching is None:
                raise ProjectInventoryError(
                    "archive_identity_unavailable",
                    "Project archive identity is not currently available",
                )
            if matching.tmux_session_running or not matching.archive_available:
                raise ProjectInventoryError(
                    "archive_running_blocked",
                    matching.archive_unavailable_reason
                    or "Archive is blocked while the project session is running",
                )
        if request.identity_kind == "project_path":
            confirmed_path = matching.project_path if matching is not None else request.project_path
            if confirmed_path is None:
                raise ProjectInventoryError("project_path_not_absolute", "Project path is required")
            record = update_project_inventory_record(
                project_id=project_id,
                project_path=confirmed_path,
                archived=request.archived,
                expected_revision=request.expected_revision,
                reason=request.reason,
                require_allowed_root=False,
            )
        elif request.identity_kind == "telegram_topic":
            if matching is None or matching.chat_id is None:
                raise ProjectInventoryError(
                    "project_id_mismatch", "Topic identity is not confirmed"
                )
            record = update_topic_inventory_record(
                project_id=project_id,
                chat_id=matching.chat_id,
                thread_id=matching.thread_id,
                archived=request.archived,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )
        else:
            if request.project_path is not None:
                raise ProjectInventoryError(
                    "project_id_mismatch", "Topic config entry identity must not include a path"
                )
            if matching is None or matching.topic_config_key is None:
                raise ProjectInventoryError(
                    "archive_identity_unavailable",
                    "Project archive identity is not currently available",
                )
            if request.archived and not matching.archive_available:
                raise ProjectInventoryError(
                    "archive_running_blocked",
                    matching.archive_unavailable_reason
                    or "Archive is blocked while the project session is running",
                )
            record = update_config_topic_inventory_record(
                project_id=project_id,
                topic_config_key=matching.topic_config_key,
                archived=request.archived,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )
    except ProjectSettingsError as exc:
        raise _settings_http_error(exc) from exc
    except ProjectInventoryError as exc:
        raise _inventory_http_error(exc) from exc
    return ProjectInventoryUpdateResponse(**record.model_dump())


@router.get("/project-settings", response_model=ProjectSettingsListResponse)
def read_project_settings() -> ProjectSettingsListResponse:
    settings = get_settings()
    try:
        store, migration_required = read_project_settings_store(settings)
    except ProjectSettingsError as exc:
        raise _settings_http_error(exc) from exc
    records = _records_from_store(store)
    bindings = list_project_bindings(settings).bindings
    runtime_store, runtime_store_invalid = _read_runtime_store_once(settings)
    projects = [
        _project_view(
            record,
            settings=settings,
            bindings=bindings,
            runtime_store=runtime_store,
            runtime_store_invalid=runtime_store_invalid,
        )
        for record in records
    ]
    return ProjectSettingsListResponse(
        store_revision=int(store["store_revision"]),
        project_count=len(projects),
        projects=projects,
        migrated_from=ProjectSettingsMigrationMetadata.model_validate(store["migrated_from"]),
        migration_required=migration_required,
    )


@router.get("/project-settings/{project_id}", response_model=ProjectSettingsGetResponse)
def read_project_setting(project_id: str) -> ProjectSettingsGetResponse:
    settings = get_settings()
    try:
        requested = validate_project_id(project_id)
        store, _ = read_project_settings_store(settings)
    except ProjectSettingsError as exc:
        raise _settings_http_error(exc) from exc
    projects = store.get("projects")
    if not isinstance(projects, dict) or requested not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "settings_not_found", "message": "Project settings not found"},
        )
    record = ProjectSettingsRecord.model_validate(projects[requested])
    bindings = list_project_bindings(settings).bindings
    runtime_store, runtime_store_invalid = _read_runtime_store_once(settings)
    view = _project_view(
        record,
        settings=settings,
        bindings=bindings,
        runtime_store=runtime_store,
        runtime_store_invalid=runtime_store_invalid,
    )
    return ProjectSettingsGetResponse(**view.model_dump())


@router.post("/project-settings/validate", response_model=ProjectSettingsValidationResponse)
def validate_project_setting(
    request: ProjectSettingsValidationRequest,
) -> ProjectSettingsValidationResponse:
    try:
        canonical_project_path(request.project_path)
        return validate_provider_project_settings(
            provider_id=request.provider_id,
            account_profile=request.account_profile,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
        )
    except ProjectSettingsError as exc:
        raise _settings_http_error(exc) from exc


@router.put("/project-settings", response_model=ProjectSettingsUpsertResponse)
def write_project_setting(request: ProjectSettingsUpsertRequest) -> ProjectSettingsUpsertResponse:
    settings = get_settings()
    try:
        validate_provider_project_settings(
            provider_id=request.provider_id,
            account_profile=request.account_profile,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            settings=settings,
        )
        record = upsert_project_settings(
            project_path=request.project_path,
            provider_id=request.provider_id,
            account_profile=request.account_profile,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            expected_revision=request.expected_revision,
            settings=settings,
        )
    except ProjectSettingsError as exc:
        raise _settings_http_error(exc) from exc
    bindings = list_project_bindings(settings).bindings
    runtime_store, runtime_store_invalid = _read_runtime_store_once(settings)
    view = _project_view(
        record,
        settings=settings,
        bindings=bindings,
        runtime_store=runtime_store,
        runtime_store_invalid=runtime_store_invalid,
    )
    return ProjectSettingsUpsertResponse(**view.model_dump())


@router.post("/project-settings/apply", response_model=ProjectSettingsApplyResponse)
def apply_project_setting(request: ProjectSettingsApplyRequest) -> ProjectSettingsApplyResponse:
    try:
        return apply_project_codex_settings(
            project_id=request.project_id,
            expected_settings_revision=request.expected_settings_revision,
            confirm_interrupt=request.confirm_interrupt,
            allow_last_known_good=request.allow_last_known_good,
        )
    except SessionRuntimeError as exc:
        raise _runtime_http_error(exc) from exc


@router.post(
    "/project-settings/apply-selection",
    response_model=ProjectSettingsApplyResponse,
)
def apply_project_selection(
    request: ProjectSettingsSelectionApplyRequest,
) -> ProjectSettingsApplyResponse:
    try:
        return apply_project_codex_selection(
            project_path=request.project_path,
            provider_id=request.provider_id,
            account_profile=request.account_profile,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
            expected_settings_revision=request.expected_settings_revision,
            confirm_interrupt=request.confirm_interrupt,
            allow_last_known_good=request.allow_last_known_good,
        )
    except SessionRuntimeError as exc:
        raise _runtime_http_error(exc) from exc


@router.put("/bindings", response_model=ProjectBinding)
def write_project_binding(request: ProjectBindingUpsertRequest) -> ProjectBinding:
    try:
        return upsert_project_binding(
            project_path=request.project_path,
            provider_id=request.provider_id,
            account_slug=request.account_slug,
        )
    except SessionRuntimeError as exc:
        code_to_status = {
            "project_missing": status.HTTP_404_NOT_FOUND,
            "profile_missing": status.HTTP_404_NOT_FOUND,
            "auth_missing": status.HTTP_409_CONFLICT,
            "provider_not_supported": status.HTTP_409_CONFLICT,
        }
        raise HTTPException(
            status_code=code_to_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.post("/switch-account", response_model=ProjectAccountSwitchResponse)
def switch_account(request: ProjectAccountSwitchRequest) -> ProjectAccountSwitchResponse:
    try:
        return switch_project_codex_account(
            project_path=request.project_path,
            account_slug=request.account_slug,
            confirm_interrupt=request.confirm_interrupt,
        )
    except SessionRuntimeError as exc:
        code_to_status = {
            "project_missing": status.HTTP_404_NOT_FOUND,
            "project_not_configured": status.HTTP_404_NOT_FOUND,
            "profile_missing": status.HTTP_404_NOT_FOUND,
            "auth_missing": status.HTTP_409_CONFLICT,
            "project_agent_not_codex": status.HTTP_409_CONFLICT,
            "session_not_running": status.HTTP_409_CONFLICT,
            "current_account_unknown": status.HTTP_409_CONFLICT,
            "confirmation_required": status.HTTP_409_CONFLICT,
            "integration_incompatible": status.HTTP_409_CONFLICT,
            "work_in_progress": status.HTTP_409_CONFLICT,
            "switch_failed_rolled_back": status.HTTP_502_BAD_GATEWAY,
            "switch_failed_rollback_failed": status.HTTP_500_INTERNAL_SERVER_ERROR,
            "control_socket_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
            "control_socket_failed": status.HTTP_503_SERVICE_UNAVAILABLE,
        }
        raise HTTPException(
            status_code=code_to_status.get(exc.code, status.HTTP_400_BAD_REQUEST),
            detail={"code": exc.code, "message": exc.message},
        ) from exc
