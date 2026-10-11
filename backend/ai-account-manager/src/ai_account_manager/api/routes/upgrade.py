from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Header, HTTPException, status

from ai_account_manager.core.config import get_settings
from ai_account_manager.core.upgrade_manager import (
    ActivationRunner,
    CandidateDescriptor,
    CompatibilityGate,
    CompatibilityGateResult,
    EngineeringResult,
    EngineeringSessionRunner,
    SafeUpgradeManager,
    TransactionResult,
    UpgradeManagerError,
    UpgradeStateStore,
    UpgradeMaintenanceStatus,
    UpgradeStatus,
)
from ai_account_manager.providers.codex.upgrade_mvp import (
    CODEX_GATE_VERSION,
    CodexActivationRunner,
    CodexCompatibilityGate,
    CodexEngineeringSessionRunner,
    JsonArtifactRunner,
)
from ai_account_manager.providers.codex.upgrade_release import CodexReleaseDiscovery
from ai_account_manager.providers.codex.upgrade_retention import (
    CodexUpgradeRetention,
    LazyCodexUpgradeRetention,
)
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    read_private_bytes,
    safe_executable,
)


router = APIRouter(prefix="/api/providers/{provider_id}/upgrade", tags=["upgrade"])


class _UnavailableAdapters(CompatibilityGate, EngineeringSessionRunner, ActivationRunner):
    def run(self, candidate: CandidateDescriptor) -> CompatibilityGateResult:
        raise UpgradeManagerError("runner_not_configured")

    def start(
        self,
        candidate: CandidateDescriptor,
        evidence_path,
        attempt: int,  # type: ignore[no-untyped-def]
    ) -> EngineeringResult:
        raise UpgradeManagerError("runner_not_configured")

    def close(self, session_id: str) -> None:
        raise UpgradeManagerError("runner_not_configured")

    def apply(self, candidate: CandidateDescriptor) -> TransactionResult:
        raise UpgradeManagerError("runner_not_configured")

    def rollback(self, candidate: CandidateDescriptor) -> TransactionResult:
        raise UpgradeManagerError("runner_not_configured")


def _require_codex(provider_id: str) -> None:
    if provider_id != "codex":
        raise UpgradeManagerError("upgrade_provider_not_supported")


def _status_manager(provider_id: str) -> SafeUpgradeManager:
    _require_codex(provider_id)
    store = UpgradeStateStore(get_settings().upgrade_runtime_root)
    unavailable = _UnavailableAdapters()
    return SafeUpgradeManager(
        store,
        unavailable,
        unavailable,
        unavailable,
        provider_id=provider_id,
        compatibility_gate_version=CODEX_GATE_VERSION,
    )


def _operation_manager(provider_id: str, operation: str) -> SafeUpgradeManager:
    _require_codex(provider_id)
    settings = get_settings()
    unavailable = _UnavailableAdapters()
    gate: CompatibilityGate = unavailable
    engineering: EngineeringSessionRunner = unavailable
    activation: ActivationRunner = unavailable
    release_discovery = None
    retention = None
    if operation == "refresh":
        retention = LazyCodexUpgradeRetention(settings)
        release_discovery = CodexReleaseDiscovery(
            settings.upgrade_runner_config_path,
            settings.upgrade_runtime_root,
            retention=retention,
        )
    if operation in {"check", "repair"}:
        gate = CodexCompatibilityGate(
            JsonArtifactRunner(
                settings.upgrade_gate_runner,
                timeout_seconds=settings.upgrade_runner_timeout_seconds,
                config_path=settings.upgrade_runner_config_path,
            )
        )
    if operation == "repair":
        retention = LazyCodexUpgradeRetention(settings)
        engineering = CodexEngineeringSessionRunner(
            JsonArtifactRunner(
                settings.upgrade_engineering_runner,
                timeout_seconds=settings.upgrade_runner_timeout_seconds,
                config_path=settings.upgrade_runner_config_path,
            )
        )
    if operation == "update":
        retention = LazyCodexUpgradeRetention(settings)
        activation = CodexActivationRunner(
            JsonArtifactRunner(
                settings.upgrade_activation_runner,
                timeout_seconds=settings.upgrade_runner_timeout_seconds,
                config_path=settings.upgrade_runner_config_path,
            )
        )
    if operation not in {"refresh", "check", "repair", "update"}:
        raise UpgradeManagerError("upgrade_operation_not_supported")
    return SafeUpgradeManager(
        UpgradeStateStore(settings.upgrade_runtime_root),
        gate,
        engineering,
        activation,
        provider_id=provider_id,
        compatibility_gate_version=CODEX_GATE_VERSION,
        release_discovery=release_discovery,
        retention=retention,
        discovery_ttl_seconds=settings.upgrade_release_refresh_ttl_seconds,
    )


def _http_error(exc: UpgradeManagerError) -> HTTPException:
    unavailable = {
        "runner_unavailable",
        "runner_execution_failed",
        "runner_result_missing",
        "runner_result_invalid",
        "runner_result_unsafe",
    }
    return HTTPException(
        status_code=(
            status.HTTP_404_NOT_FOUND
            if exc.code == "upgrade_provider_not_supported"
            else (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code in unavailable
                else status.HTTP_409_CONFLICT
            )
        ),
        detail={"code": exc.code},
    )


def _validated_operation_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise UpgradeManagerError("operation_id_invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise UpgradeManagerError("operation_id_invalid")
    return value


def _runner_identity(operation: str) -> dict[str, str]:
    settings = get_settings()
    runner = {
        "check": settings.upgrade_gate_runner,
        "update": settings.upgrade_activation_runner,
    }[operation]
    try:
        executable = safe_executable(runner, code="RUNNER_IDENTITY_UNAVAILABLE")
        config = read_private_bytes(
            settings.upgrade_runner_config_path,
            code="RUNNER_CONFIG_IDENTITY_UNAVAILABLE",
        )
    except OperationalError as exc:
        raise UpgradeManagerError(exc.code) from exc
    return {
        "runner_path": str(executable),
        "runner_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "config_path": str(settings.upgrade_runner_config_path),
        "config_sha256": hashlib.sha256(config).hexdigest(),
    }


@router.get("/status", response_model=UpgradeStatus)
def get_upgrade_status(provider_id: str) -> UpgradeStatus:
    try:
        return _status_manager(provider_id).status()
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.get("/maintenance", response_model=UpgradeMaintenanceStatus)
def get_upgrade_maintenance_status(provider_id: str) -> UpgradeMaintenanceStatus:
    try:
        _require_codex(provider_id)
        return UpgradeMaintenanceStatus.model_validate(CodexUpgradeRetention().public_status())
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/refresh", response_model=UpgradeStatus)
def refresh_release(provider_id: str) -> UpgradeStatus:
    try:
        return _operation_manager(provider_id, "refresh").refresh_release()
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/check", response_model=UpgradeStatus)
def check_compatibility(
    provider_id: str,
    operation_id: str | None = Header(default=None, alias="X-CUS-Operation-Id"),
) -> UpgradeStatus:
    try:
        return _operation_manager(provider_id, "check").check(
            operation_id=_validated_operation_id(operation_id),
            runner_identity=_runner_identity("check") if operation_id else None,
        )
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/repair/prepare", response_model=UpgradeStatus)
def prepare_repair_evidence(provider_id: str) -> UpgradeStatus:
    try:
        return _status_manager(provider_id).prepare()
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/repair", response_model=UpgradeStatus)
def repair_compatibility(provider_id: str) -> UpgradeStatus:
    try:
        return _operation_manager(provider_id, "repair").repair()
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc


@router.post("/update", response_model=UpgradeStatus)
def activate_qualified_candidate(
    provider_id: str,
    operation_id: str | None = Header(default=None, alias="X-CUS-Operation-Id"),
) -> UpgradeStatus:
    try:
        return _operation_manager(provider_id, "update").update(
            operation_id=_validated_operation_id(operation_id),
            runner_identity=_runner_identity("update") if operation_id else None,
        )
    except UpgradeManagerError as exc:
        raise _http_error(exc) from exc
