from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.core.upgrade_manager import (
    REQUIRED_ACTIVATION_STEPS,
    REQUIRED_GATE_CHECKS,
    REQUIRED_ROLLBACK_STEPS,
    CandidateBinding,
    CandidateDescriptor,
    GateVerdict,
    StepStatus,
    TransactionResult,
    UpgradePipelineRecord,
    UpgradeState,
)
from ai_account_manager.providers.codex.upgrade_candidate import profile_preflight, verify_candidate
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalConfig,
    OperationalError,
    append_ledger,
    assert_private_directory,
    atomic_write_bytes,
    atomic_write_model,
    canonical_json,
    canonical_path,
    clean_environment,
    file_sha256,
    protocol_main,
    read_private_bytes,
    read_private_model,
    resolve_active_launcher,
    run_bounded,
    safe_executable,
)
from ai_account_manager.providers.codex.upgrade_overlay import tree_digest
from ai_account_manager.providers.codex.upgrade_runtime_spec import reconcile_runtime_spec
from ai_account_manager.providers.codex.upgrade_telegram_e2e_contract import TelegramE2EResult


POST_UPGRADE_MARKER = "CUS_CODEX_POST_UPGRADE_FINAL_148"
POST_UPGRADE_RESUME_MARKER = "CUS_CODEX_POST_UPGRADE_RESUME_148"


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate: CandidateDescriptor


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_binding: CandidateBinding


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    binding: CandidateBinding
    files: dict[str, str] = Field(min_length=8)
    running_sessions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    known_good_binary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_launcher_target: str


def _binding_key(binding: CandidateBinding) -> str:
    return hashlib.sha256(canonical_json(binding)).hexdigest()


def _load_json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(read_private_bytes(path, code=code))
    except json.JSONDecodeError as exc:
        raise OperationalError(code) from exc
    if not isinstance(payload, dict):
        raise OperationalError(code)
    return payload


def _managed_paths(config: OperationalConfig) -> dict[str, Path]:
    return {
        "active_reference": Path(config.active_reference),
        "runtime_specs": Path(config.runtime_specs_path),
        "project_state": Path(config.project_state_path),
        "compatibility_state": Path(config.compatibility_state_path),
        "overlay_reference": Path(config.overlay_active_reference),
        "pth": Path(config.pth_path),
        "service_state": Path(config.service_state_path),
        "sessions_state": Path(config.sessions_state_path),
    }


def _replace_launcher(path: Path, target: Path) -> None:
    resolve_active_launcher(path, code="ACTIVE_LAUNCHER_UNSAFE")
    executable = safe_executable(target, code="ACTIVE_LAUNCHER_TARGET_UNSAFE")
    temporary = path.with_name(f".{path.name}-{os.getpid()}-{time.monotonic_ns()}")
    try:
        temporary.symlink_to(executable)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise OperationalError("ACTIVE_LAUNCHER_UPDATE_FAILED") from exc
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _snapshot(
    config: OperationalConfig, candidate: CandidateDescriptor
) -> tuple[Path, SnapshotManifest]:
    transaction = assert_private_directory(
        Path(config.deployment_root) / "transactions" / _binding_key(candidate.binding),
        create=True,
        code="TRANSACTION_ROOT_UNSAFE",
    )
    snapshot = assert_private_directory(
        transaction / "snapshot", create=True, code="TRANSACTION_ROOT_UNSAFE"
    )
    manifest_path = transaction / "snapshot-manifest.json"
    if manifest_path.exists():
        manifest = read_private_model(
            manifest_path, SnapshotManifest, code="SNAPSHOT_MANIFEST_INVALID"
        )
        if manifest.binding != candidate.binding:
            raise OperationalError("SNAPSHOT_BINDING_MISMATCH")
        return transaction, manifest
    files: dict[str, str] = {}
    for name, source in _managed_paths(config).items():
        canonical_path(source, code="DEPLOYMENT_STATE_UNSAFE")
        payload = read_private_bytes(source, code="DEPLOYMENT_STATE_UNSAFE")
        atomic_write_bytes(snapshot / name, payload)
        files[name] = hashlib.sha256(payload).hexdigest()
    active = _load_json(Path(config.active_reference), code="ACTIVE_REFERENCE_INVALID")
    binary_path = active.get("binary_path")
    binary_digest = active.get("sha256")
    if not isinstance(binary_path, str) or not isinstance(binary_digest, str):
        raise OperationalError("ACTIVE_REFERENCE_INVALID")
    if file_sha256(Path(binary_path), code="KNOWN_GOOD_BINARY_UNSAFE") != binary_digest:
        raise OperationalError("KNOWN_GOOD_BINARY_DIGEST_MISMATCH")
    launcher_target = resolve_active_launcher(
        Path(config.active_launcher_path), code="ACTIVE_LAUNCHER_UNSAFE"
    )
    if launcher_target != Path(binary_path).resolve(strict=True):
        raise OperationalError("ACTIVE_LAUNCHER_REFERENCE_MISMATCH")
    sessions_digest = file_sha256(
        Path(config.sessions_state_path), code="SESSIONS_STATE_UNSAFE"
    )
    manifest = SnapshotManifest(
        binding=candidate.binding,
        files=files,
        running_sessions_sha256=sessions_digest,
        known_good_binary_sha256=binary_digest,
        active_launcher_target=str(launcher_target),
    )
    atomic_write_model(manifest_path, manifest)
    return transaction, manifest


def _steps(required: tuple[str, ...], passed: list[str]) -> dict[str, StepStatus]:
    return {item: StepStatus.PASS if item in passed else StepStatus.ERROR for item in required}


def _fault(configured: str | None, step: str) -> bool:
    return configured is not None and configured == step


def _require_compatibility(candidate: CandidateDescriptor, config: OperationalConfig) -> None:
    state = read_private_model(
        Path(config.compatibility_state_path),
        UpgradePipelineRecord,
        code="COMPATIBILITY_STATE_INVALID",
    )
    compatibility = state.compatibility_pass
    gate = state.last_gate
    checks = compatibility.checks if compatibility is not None else []
    expected_states = (
        {UpgradeState.UPDATING}
        if config.production_enabled
        else {UpgradeState.COMPATIBLE, UpgradeState.UPDATING}
    )
    if (
        state.state not in expected_states
        or state.candidate != candidate
        or compatibility is None
        or compatibility.binding != candidate.binding
        or gate is None
        or gate.verdict is not GateVerdict.COMPATIBLE
        or gate.binding != candidate.binding
        or gate.failure_codes
        or len(checks) != len(REQUIRED_GATE_CHECKS)
        or {check.check_id for check in checks} != set(REQUIRED_GATE_CHECKS)
        or any(check.status is not StepStatus.PASS or check.failure_code for check in checks)
    ):
        raise OperationalError("EXACT_COMPATIBILITY_PASS_REQUIRED")


def _verify_fresh_overlay_import(
    candidate: CandidateDescriptor, config: OperationalConfig
) -> dict[str, object]:
    overlay = canonical_path(Path(config.overlay_root), code="OVERLAY_ROOT_UNSAFE")
    if tree_digest(overlay) != candidate.binding.combined_overlay_identity:
        raise OperationalError("OVERLAY_IDENTITY_MISMATCH")
    python = safe_executable(
        Path(config.telegram_python),
        code="TELEGRAM_PYTHON_UNSAFE",
        allowed_root=Path(config.root),
    )
    probe = (
        "import json,sys,telegram_bot; "
        "import telegram_bot.core.services.codex_rollout as rollout; "
        "print(json.dumps({'package':telegram_bot.__file__,'rollout':rollout.__file__,"
        "'sys_path':sys.path},sort_keys=True))"
    )
    command = [str(python), "-I", "-c", probe]
    result = run_bounded(
        command,
        cwd=Path(config.root),
        env=clean_environment(Path(config.root) / "activation-import-home"),
        timeout_seconds=60,
        maximum_output=64 * 1024,
    )
    append_ledger(
        config,
        runner="activation",
        operation="fresh-overlay-import-origin",
        argv=[str(python), "-I", "-c", "<fixed import-origin and precedence probe>"],
        cwd=Path(config.root),
        result=result,
        evidence_class="PROCESS_INTEGRATION",
        result_code="PASS" if result.returncode == 0 else "FAILED",
    )
    if result.returncode != 0:
        raise OperationalError("OVERLAY_IMPORT_FAILED")
    try:
        observation = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OperationalError("OVERLAY_IMPORT_FAILED") from exc
    package = Path(str(observation.get("package"))).resolve(strict=True)
    rollout = Path(str(observation.get("rollout"))).resolve(strict=True)
    paths = observation.get("sys_path")
    overlay_src = (overlay / "src").resolve(strict=True)
    if (
        not package.is_relative_to(overlay)
        or not rollout.is_relative_to(overlay)
        or not isinstance(paths, list)
        or str(overlay_src) not in paths
    ):
        raise OperationalError("TELEGRAM_IMPORT_ORIGIN_MISMATCH")
    return {
        "package": str(package),
        "rollout": str(rollout),
        "overlay_src_precedence": paths.index(str(overlay_src)),
        "overlay_identity": candidate.binding.combined_overlay_identity,
    }


def _post_upgrade_e2e(
    candidate: CandidateDescriptor, config: OperationalConfig
) -> TelegramE2EResult:
    if config.qualification_fixture:
        if config.fixture_candidate_binary is None:
            raise OperationalError("QUALIFICATION_FIXTURE_INVALID")
        selected = safe_executable(
            Path(config.fixture_candidate_binary), code="CANARY_BINARY_UNSAFE"
        )
        profile = assert_private_directory(
            Path(config.root) / "activation-fixture-profile",
            create=True,
            code="CANARY_PROFILE_UNSAFE",
        )
        fixture_scenario: str | None = "normal"
        evidence_class = "DISPOSABLE_E2E"
    else:
        preflight = profile_preflight(
            Path(config.approved_codex_binary),
            Path(config.dedicated_test_profile),
            Path(config.root) / "activation-profile-home",
        )
        if preflight.status != "PROFILE_READY":
            raise OperationalError("DEDICATED_NON_PRODUCTION_CODEX_PROFILE")
        selected = safe_executable(Path(candidate.binary_path), code="CANDIDATE_BINARY_UNSAFE")
        profile = Path(config.dedicated_test_profile)
        fixture_scenario = None
        evidence_class = "REAL_CANDIDATE_E2E"
    root = assert_private_directory(
        Path(config.root) / "post-upgrade-e2e",
        create=True,
        code="POST_UPGRADE_E2E_UNSAFE",
    )
    request_path = root / "request.json"
    result_path = root / "result.json"
    atomic_write_model(
        request_path,
        {
            "schema_version": 1,
            "candidate_binary": str(selected),
            "candidate_sha256": file_sha256(selected, code="CANARY_BINARY_UNSAFE"),
            "profile": str(profile),
            "cwd": str(root),
            "overlay_root": config.overlay_root,
            "project_state_path": config.project_state_path,
            "model": config.canary_model,
            "reasoning_effort": config.canary_reasoning_effort,
            "final_marker": POST_UPGRADE_MARKER,
            "resume_marker": POST_UPGRADE_RESUME_MARKER,
            "fixture_scenario": fixture_scenario,
        },
    )
    python = safe_executable(
        Path(config.telegram_python),
        code="TELEGRAM_PYTHON_UNSAFE",
        allowed_root=Path(config.root),
    )
    command = [
        str(python),
        "-m",
        "ai_account_manager.providers.codex.upgrade_telegram_e2e",
        str(request_path),
        str(result_path),
    ]
    result = run_bounded(
        command,
        cwd=root,
        env=clean_environment(root / "process-home"),
        timeout_seconds=600,
    )
    append_ledger(
        config,
        runner="activation",
        operation="post-upgrade-telegram-agent-e2e",
        argv=command,
        cwd=root,
        result=result,
        evidence_class=evidence_class,  # type: ignore[arg-type]
        result_code="PASS" if result.returncode == 0 else "FAILED",
    )
    if result.returncode != 0:
        raise OperationalError("POST_UPGRADE_E2E_RUNNER_FAILED")
    observed = read_private_model(
        result_path, TelegramE2EResult, code="POST_UPGRADE_E2E_RESULT_INVALID"
    )
    if (
        observed.verdict != "PASS"
        or observed.final_marker_deliveries != 1
        or observed.resume_marker_deliveries != 1
        or not observed.send_lifecycle_completed
        or observed.stopped_projects_started
    ):
        raise OperationalError("POST_UPGRADE_E2E_FAILED")
    return observed


def execute_apply(request: ApplyRequest, config: OperationalConfig) -> TransactionResult:
    candidate = request.candidate
    verify_candidate(candidate, Path(config.manager_runtime_root) / "candidate-provenance.json")
    _require_compatibility(candidate, config)
    passed: list[str] = []
    transaction, snapshot = _snapshot(config, candidate)
    passed.append("rollback_bundle_created")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    atomic_write_model(
        Path(config.active_reference),
        {"binary_path": candidate.binary_path, "sha256": candidate.binding.digest_sha256},
    )
    _replace_launcher(Path(config.active_launcher_path), Path(candidate.binary_path))
    passed.append("candidate_activated")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    regenerated = reconcile_runtime_spec(
        Path(config.runtime_specs_path),
        candidate,
        account_profile="codex-upgrade-qualification",
        model=config.canary_model,
        reasoning_effort=config.canary_reasoning_effort,
    )
    if (
        regenerated.candidate_command != candidate.binary_path
        or regenerated.candidate_digest != candidate.binding.digest_sha256
        or regenerated.stopped_projects_started is not False
    ):
        raise OperationalError("RUNTIME_SPEC_STALE")
    passed.append("runtime_specs_reconciled")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    projects = _load_json(Path(config.project_state_path), code="PROJECT_STATE_INVALID")
    values = projects.get("projects")
    if not isinstance(values, dict) or any(
        isinstance(item, dict) and item.get("before") == "stopped" and item.get("after") != "stopped"
        for item in values.values()
    ):
        raise OperationalError("STOPPED_PROJECT_STARTED")
    passed.append("stopped_projects_not_started")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    atomic_write_model(
        Path(config.overlay_active_reference),
        {"identity": candidate.binding.combined_overlay_identity, "root": config.overlay_root},
    )
    atomic_write_bytes(Path(config.pth_path), f"{config.overlay_root}/src\n".encode())
    passed.append("combined_overlay_activated")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    active = _load_json(Path(config.active_reference), code="ACTIVE_REFERENCE_INVALID")
    if (
        active.get("binary_path") != candidate.binary_path
        or file_sha256(Path(candidate.binary_path), code="CANDIDATE_BINARY_UNSAFE")
        != candidate.binding.digest_sha256
        or resolve_active_launcher(
            Path(config.active_launcher_path), code="ACTIVE_LAUNCHER_UNSAFE"
        )
        != Path(candidate.binary_path).resolve(strict=True)
    ):
        raise OperationalError("BINARY_ORIGIN_MISMATCH")
    passed.append("binary_origin_verified")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    overlay = _load_json(
        Path(config.overlay_active_reference), code="OVERLAY_REFERENCE_INVALID"
    )
    if overlay.get("root") != config.overlay_root or not Path(config.overlay_root).is_dir():
        raise OperationalError("TELEGRAM_IMPORT_ORIGIN_MISMATCH")
    _verify_fresh_overlay_import(candidate, config)
    passed.append("telegram_bot_import_origin_verified")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    services = _load_json(Path(config.service_state_path), code="SERVICE_STATE_INVALID")
    if not services or any(value != "healthy" for value in services.values()):
        raise OperationalError("DISPOSABLE_SERVICES_UNHEALTHY")
    passed.append("services_healthy")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    _post_upgrade_e2e(candidate, config)
    passed.append("post_upgrade_smoke")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    if (
        file_sha256(Path(config.sessions_state_path), code="SESSIONS_STATE_UNSAFE")
        != snapshot.running_sessions_sha256
    ):
        raise OperationalError("RUNNING_SESSION_PRESERVATION_UNPROVEN")
    passed.append("running_sessions_preserved")
    if _fault(config.activation_fault_after, passed[-1]):
        return TransactionResult(
            binding=candidate.binding,
            steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
            failure_code="INJECTED_ACTIVATION_FAILURE",
        )
    atomic_write_model(
        transaction / "transaction-state.json",
        {"state": "ACTIVE", "binding": candidate.binding.model_dump(mode="json")},
    )
    return TransactionResult(
        binding=candidate.binding,
        steps=_steps(REQUIRED_ACTIVATION_STEPS, passed),
    )


def _recovery_evidence(
    config: OperationalConfig,
    binding: CandidateBinding,
    transaction: Path,
    passed: list[str],
) -> None:
    root = assert_private_directory(
        Path(config.root) / "recovery-evidence", create=True, code="RECOVERY_EVIDENCE_UNSAFE"
    )
    atomic_write_model(
        root / f"{_binding_key(binding)}.json",
        {
            "schema_version": 1,
            "state": "BLOCKED",
            "binding": binding.model_dump(mode="json"),
            "transaction": str(transaction),
            "rollback_steps_completed": passed,
        },
    )


def execute_rollback(request: RollbackRequest, config: OperationalConfig) -> TransactionResult:
    binding = request.candidate_binding
    transaction = Path(config.deployment_root) / "transactions" / _binding_key(binding)
    manifest = read_private_model(
        transaction / "snapshot-manifest.json",
        SnapshotManifest,
        code="SNAPSHOT_MANIFEST_INVALID",
    )
    if manifest.binding != binding:
        raise OperationalError("SNAPSHOT_BINDING_MISMATCH")
    snapshot = transaction / "snapshot"
    passed: list[str] = []

    def restore(name: str, target: Path) -> None:
        payload = read_private_bytes(snapshot / name, code="SNAPSHOT_FILE_INVALID")
        if hashlib.sha256(payload).hexdigest() != manifest.files[name]:
            raise OperationalError("SNAPSHOT_DIGEST_MISMATCH")
        atomic_write_bytes(target, payload)

    restore("active_reference", Path(config.active_reference))
    _replace_launcher(
        Path(config.active_launcher_path), Path(manifest.active_launcher_target)
    )
    passed.append("codex_restored")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    restore("runtime_specs", Path(config.runtime_specs_path))
    restore("project_state", Path(config.project_state_path))
    passed.append("runtime_specs_restored")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    restore("overlay_reference", Path(config.overlay_active_reference))
    restore("pth", Path(config.pth_path))
    passed.append("overlay_restored")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    restore("compatibility_state", Path(config.compatibility_state_path))
    passed.append("compatibility_state_restored")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    active = _load_json(Path(config.active_reference), code="ACTIVE_REFERENCE_INVALID")
    binary_path = active.get("binary_path")
    if (
        not isinstance(binary_path, str)
        or active.get("sha256") != manifest.known_good_binary_sha256
        or file_sha256(Path(binary_path), code="KNOWN_GOOD_BINARY_UNSAFE")
        != manifest.known_good_binary_sha256
        or resolve_active_launcher(
            Path(config.active_launcher_path), code="ACTIVE_LAUNCHER_UNSAFE"
        )
        != Path(manifest.active_launcher_target).resolve(strict=True)
    ):
        raise OperationalError("ROLLBACK_ORIGIN_MISMATCH")
    for name, target in _managed_paths(config).items():
        if name == "service_state":
            continue
        if file_sha256(target, code="RESTORED_STATE_UNSAFE") != manifest.files[name]:
            raise OperationalError("ROLLBACK_DIGEST_MISMATCH")
    passed.append("origins_verified")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    restore("service_state", Path(config.service_state_path))
    restore("sessions_state", Path(config.sessions_state_path))
    services = _load_json(Path(config.service_state_path), code="SERVICE_STATE_INVALID")
    if not services or any(value != "healthy" for value in services.values()):
        raise OperationalError("ROLLBACK_SERVICES_UNHEALTHY")
    passed.append("services_healthy")
    if _fault(config.rollback_fault_after, passed[-1]):
        _recovery_evidence(config, binding, transaction, passed)
        return TransactionResult(
            binding=binding,
            steps=_steps(REQUIRED_ROLLBACK_STEPS, passed),
            failure_code="INJECTED_ROLLBACK_FAILURE",
        )
    atomic_write_model(
        transaction / "transaction-state.json",
        {"state": "ROLLBACK", "binding": binding.model_dump(mode="json")},
    )
    return TransactionResult(binding=binding, steps=_steps(REQUIRED_ROLLBACK_STEPS, passed))


def _handler(operation: str, request_path: Path, config: OperationalConfig) -> BaseModel:
    if operation == "apply":
        return execute_apply(
            read_private_model(request_path, ApplyRequest, code="RUNNER_REQUEST_INVALID"), config
        )
    if operation == "rollback":
        return execute_rollback(
            read_private_model(request_path, RollbackRequest, code="RUNNER_REQUEST_INVALID"), config
        )
    raise OperationalError("OPERATION_UNSUPPORTED")


def main() -> None:
    raise SystemExit(protocol_main("activation", _handler, sys.argv))


if __name__ == "__main__":
    main()
