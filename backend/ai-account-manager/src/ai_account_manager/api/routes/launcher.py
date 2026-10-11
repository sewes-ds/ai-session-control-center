from fastapi import APIRouter, HTTPException, status

from ai_account_manager.core.launcher import (
    LauncherResolveError,
    build_project_launch_spec,
    resolve_launcher,
)
from ai_account_manager.schemas.launcher import (
    ImmutableLaunchSpec,
    LauncherResolveRequest,
    LauncherResolveResponse,
    ProjectSettingsResolveRequest,
)

router = APIRouter(prefix="/api/launcher", tags=["launcher"])


def _launcher_error_to_http(exc: LauncherResolveError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if exc.code in {"project_missing", "settings_missing", "profile_missing"}:
        status_code = status.HTTP_404_NOT_FOUND
    elif exc.code == "settings_store_invalid":
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    elif exc.code in {
        "settings_incomplete",
        "revision_mismatch",
        "capabilities_current_required",
        "capabilities_refresh_required",
        "model_not_available",
        "reasoning_not_supported",
        "provider_settings_not_supported",
    }:
        status_code = status.HTTP_409_CONFLICT
    elif exc.code == "provider_not_implemented":
        status_code = status.HTTP_501_NOT_IMPLEMENTED
    elif exc.code == "provider_unknown":
        status_code = status.HTTP_404_NOT_FOUND
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message})


@router.post("/resolve", response_model=LauncherResolveResponse)
def resolve_launch_spec(request: LauncherResolveRequest) -> LauncherResolveResponse:
    """Resolve command/cwd/env for a future tmux-bot launch without starting anything."""
    try:
        return resolve_launcher(request)
    except LauncherResolveError as exc:
        raise _launcher_error_to_http(exc) from exc


@router.post("/resolve-project-settings", response_model=ImmutableLaunchSpec)
def resolve_project_settings(request: ProjectSettingsResolveRequest) -> ImmutableLaunchSpec:
    """Resolve an immutable launch spec from saved project settings without mutating runtime state."""
    try:
        return build_project_launch_spec(
            project_id=request.project_id,
            expected_settings_revision=request.expected_settings_revision,
            allow_last_known_good=request.allow_last_known_good,
        )
    except LauncherResolveError as exc:
        raise _launcher_error_to_http(exc) from exc
