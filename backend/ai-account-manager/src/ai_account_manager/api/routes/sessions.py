from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from ai_account_manager.core.project_inventory import (
    ProjectInventoryError,
    update_config_topic_inventory_record,
    update_project_inventory_record,
    update_topic_inventory_record,
)
from ai_account_manager.integrations.project_sessions import observe_project_sessions
from ai_account_manager.schemas.sessions import (
    ObservedProjectSessionsResponse,
    ProjectInventoryUpdateRequest,
    ProjectInventoryUpdateResponse,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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


@router.get("/projects", response_model=ObservedProjectSessionsResponse)
def list_observed_projects() -> ObservedProjectSessionsResponse:
    return observe_project_sessions()


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
            record = update_config_topic_inventory_record(
                project_id=project_id,
                topic_config_key=matching.topic_config_key,
                archived=request.archived,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )
    except ProjectInventoryError as exc:
        raise _inventory_http_error(exc) from exc
    return ProjectInventoryUpdateResponse(**record.model_dump())
