from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ai_account_manager.core.upgrade_manager import (
    REQUIRED_GATE_CHECKS,
    CandidateDescriptor,
    CheckEvidence,
    CompatibilityGateResult,
    GateVerdict,
    StepStatus,
)
from ai_account_manager.providers.codex.upgrade_candidate import (
    ProfilePreflight,
    profile_preflight,
    verify_candidate,
)
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalConfig,
    OperationalError,
    append_ledger,
    assert_private_directory,
    atomic_write_model,
    clean_environment,
    file_sha256,
    protocol_main,
    read_private_bytes,
    read_private_model,
    run_bounded,
    safe_executable,
)
from ai_account_manager.providers.codex.upgrade_overlay import tree_digest
from ai_account_manager.providers.codex.upgrade_runtime_spec import read_runtime_spec
from ai_account_manager.providers.codex.upgrade_telegram_e2e_contract import TelegramE2EResult


FINAL_MARKER = "CUS_CODEX_QUALIFICATION_FINAL_148"
COMMENTARY_MARKER = "CUS_CODEX_QUALIFICATION_COMMENTARY"


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate: CandidateDescriptor


class _ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finals: list[str]
    commentary: list[str]
    ignored: int
    import_origin: str
    machine_verdict: Literal["PASS", "CHECK_FAILED"]
    failure_codes: list[str]
    completion_seen: bool


def _check(check_id: str, status: StepStatus, failure: str | None = None) -> CheckEvidence:
    return CheckEvidence(check_id=check_id, status=status, failure_code=failure)


def _candidate_version(candidate: CandidateDescriptor, config: OperationalConfig) -> None:
    binary = safe_executable(Path(candidate.binary_path), code="CANDIDATE_BINARY_UNSAFE")
    home = assert_private_directory(
        Path(config.root) / "candidate-version-home",
        create=True,
        code="CANDIDATE_VERSION_HOME_UNSAFE",
    )
    command = [str(binary), "--version"]
    result = run_bounded(
        command,
        cwd=home,
        env=clean_environment(home, extra={"CODEX_HOME": str(home)}),
        timeout_seconds=30,
        maximum_output=16 * 1024,
    )
    append_ledger(
        config,
        runner="compatibility-gate",
        operation="candidate-version",
        argv=command,
        cwd=home,
        result=result,
        evidence_class="PROCESS_INTEGRATION",
        result_code="PASS" if result.returncode == 0 else "FAILED",
    )
    expected = f"codex-cli {candidate.binding.version}"
    if result.returncode != 0 or result.stdout.decode("utf-8", errors="strict").strip() != expected:
        raise OperationalError("CANDIDATE_VERSION_MISMATCH")


def _runtime_spec(candidate: CandidateDescriptor, config: OperationalConfig) -> None:
    spec = read_runtime_spec(Path(config.runtime_specs_path))
    binding = candidate.binding
    if (
        spec.candidate_command != candidate.binary_path
        or spec.candidate_digest != binding.digest_sha256
        or spec.candidate_version != binding.version
        or spec.model != config.canary_model
        or spec.reasoning_effort != config.canary_reasoning_effort
        or spec.stopped_projects_started is not False
    ):
        raise OperationalError("RUNTIME_SPEC_STALE")


def _fixture_records() -> list[dict[str, object]]:
    final = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "id": "final-marker-1",
            "content": [{"type": "output_text", "text": FINAL_MARKER}],
        },
    }
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "id": "commentary-1",
                "content": [{"type": "output_text", "text": COMMENTARY_MARKER}],
            },
        },
        {"type": "event_msg", "payload": {"type": "reasoning", "text": "internal"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "tool"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "system", "content": []},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "not final"}],
            },
        },
        final,
        final,
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]


def _run_probe(
    config: OperationalConfig, records: list[dict[str, object]], *, name: str
) -> _ProbeResult:
    root = assert_private_directory(
        Path(config.root) / "telegram-probes", create=True, code="TELEGRAM_PROBE_UNSAFE"
    )
    request = root / f"{name}-request.json"
    result_path = root / f"{name}-result.json"
    atomic_write_model(request, records)
    python = safe_executable(
        Path(config.telegram_python), code="TELEGRAM_PYTHON_UNSAFE", allowed_root=Path(config.root)
    )
    command = [
        str(python),
        "-m",
        "ai_account_manager.providers.codex.upgrade_telegram_probe",
        str(request),
        str(result_path),
    ]
    command_result = run_bounded(
        command,
        cwd=Path(config.root),
        env=clean_environment(Path(config.root) / "telegram-probe-home"),
        timeout_seconds=60,
    )
    append_ledger(
        config,
        runner="compatibility-gate",
        operation=f"telegram-probe-{name}",
        argv=command,
        cwd=Path(config.root),
        result=command_result,
        evidence_class="PROCESS_INTEGRATION",
        result_code="PASS" if command_result.returncode == 0 else "FAILED",
    )
    if command_result.returncode != 0:
        raise OperationalError("TELEGRAM_PROBE_FAILED")
    return read_private_model(result_path, _ProbeResult, code="TELEGRAM_PROBE_INVALID")


def _classification_checks(config: OperationalConfig) -> dict[str, tuple[bool, str | None]]:
    result = _run_probe(config, _fixture_records(), name="classification")
    missing = _run_probe(
        config,
        [{"type": "event_msg", "payload": {"type": "task_complete"}}],
        name="missing-final",
    )
    unknown = _run_probe(
        config,
        [{"type": "future_unknown_schema", "payload": {"type": "message"}}],
        name="unknown-schema",
    )
    overlay = Path(config.overlay_root).resolve()
    origin = Path(result.import_origin).resolve()
    return {
        "reasoning_not_final": (
            result.machine_verdict == "PASS"
            and result.ignored >= 4
            and FINAL_MARKER not in result.commentary,
            None,
        ),
        "tool_not_final": (result.machine_verdict == "PASS" and result.ignored >= 4, None),
        "system_not_final": (result.machine_verdict == "PASS" and result.ignored >= 4, None),
        "duplicate_final_suppressed": (
            result.machine_verdict == "PASS" and result.finals == [FINAL_MARKER],
            None,
        ),
        "completion_without_final_fails": (
            missing.machine_verdict == "CHECK_FAILED"
            and "COMPLETION_WITHOUT_FINAL" in missing.failure_codes,
            None,
        ),
        "telegram_bot_import_origin": (origin.is_relative_to(overlay), None),
        "rollout_schema_known": (
            result.machine_verdict == "PASS"
            and unknown.machine_verdict == "CHECK_FAILED"
            and "UNKNOWN_ROLLOUT_SCHEMA" in unknown.failure_codes,
            None,
        ),
    }


def _profile(config: OperationalConfig) -> ProfilePreflight:
    return profile_preflight(
        Path(config.approved_codex_binary),
        Path(config.dedicated_test_profile),
        Path(config.root) / "profile-preflight-home",
    )


def _real_canary(
    config: OperationalConfig, candidate: CandidateDescriptor
) -> tuple[dict[str, tuple[bool, str | None]], TelegramE2EResult | None]:
    check_ids = (
        "telegram_to_candidate_input",
        "candidate_to_telegram_final",
        "session_resume_history",
        "f4_runtime_truth",
    )
    preflight = _profile(config)
    if preflight.status != "PROFILE_READY" and not config.qualification_fixture:
        return (
            {item: (False, "DEDICATED_NON_PRODUCTION_CODEX_PROFILE") for item in check_ids},
            None,
        )
    root = assert_private_directory(
        Path(config.root) / ("fixture-canary" if config.qualification_fixture else "real-canary"),
        create=True,
        code="REAL_CANARY_ROOT_UNSAFE",
    )
    request_path = root / "request.json"
    result_path = root / "result.json"
    selected_binary = (
        Path(config.fixture_candidate_binary)
        if config.qualification_fixture and config.fixture_candidate_binary is not None
        else Path(candidate.binary_path)
    )
    selected_binary = safe_executable(selected_binary, code="CANARY_BINARY_UNSAFE")
    selected_digest = file_sha256(selected_binary, code="CANARY_BINARY_UNSAFE")
    profile = (
        assert_private_directory(root / "profile", create=True, code="CANARY_PROFILE_UNSAFE")
        if config.qualification_fixture
        else Path(config.dedicated_test_profile)
    )
    atomic_write_model(
        request_path,
        {
            "schema_version": 1,
            "candidate_binary": str(selected_binary),
            "candidate_sha256": selected_digest,
            "profile": str(profile),
            "cwd": str(root),
            "overlay_root": config.overlay_root,
            "project_state_path": config.project_state_path,
            "model": config.canary_model,
            "reasoning_effort": config.canary_reasoning_effort,
            "final_marker": FINAL_MARKER,
            "resume_marker": "CUS_CODEX_QUALIFICATION_RESUME_148",
            "fixture_scenario": "normal" if config.qualification_fixture else None,
        },
    )
    python = safe_executable(
        Path(config.telegram_python), code="TELEGRAM_PYTHON_UNSAFE", allowed_root=Path(config.root)
    )
    command = [
        str(python),
        "-m",
        "ai_account_manager.providers.codex.upgrade_telegram_e2e",
        str(request_path),
        str(result_path),
    ]
    execution = run_bounded(
        command,
        cwd=root,
        env=clean_environment(root / "process-home"),
        timeout_seconds=600,
    )
    append_ledger(
        config,
        runner="compatibility-gate",
        operation="telegram-agent-process-e2e",
        argv=command,
        cwd=root,
        result=execution,
        evidence_class=(
            "PROCESS_INTEGRATION" if config.qualification_fixture else "REAL_CANDIDATE_E2E"
        ),
        result_code="PASS" if execution.returncode == 0 else "FAILED",
    )
    if execution.returncode != 0:
        raise OperationalError("REAL_CANARY_RUNNER_FAILED")
    canary = read_private_model(result_path, TelegramE2EResult, code="REAL_CANARY_RESULT_INVALID")
    failure = canary.failure_codes[0] if canary.failure_codes else None
    checks = {
        "telegram_to_candidate_input": (
            canary.verdict == "PASS" and canary.queue_accepted,
            failure,
        ),
        "candidate_to_telegram_final": (
            canary.final_marker_deliveries == 1
            and canary.send_lifecycle_completed
            and canary.duplicate_final_suppressed,
            failure,
        ),
        "session_resume_history": (
            bool(canary.session_id) and canary.session_id == canary.resumed_session_id,
            failure,
        ),
        "f4_runtime_truth": (
            canary.runtime_selection_status == "current"
            and canary.candidate_command == str(selected_binary)
            and canary.candidate_sha256 == selected_digest
            and canary.observed_model == config.canary_model
            and canary.observed_reasoning_effort == config.canary_reasoning_effort
            and canary.stopped_projects_started is False
            and canary.pane_pid is not None,
            failure,
        ),
    }
    return checks, canary


_DETERMINISTIC_INCOMPATIBILITY = {
    "CANDIDATE_VERSION_MISMATCH",
    "RUNTIME_SPEC_STALE",
    "TELEGRAM_CLASSIFICATION_FAILED",
    "OVERLAY_IDENTITY_MISMATCH",
    "CANONICAL_MANIFEST_MISMATCH",
    "COMPLETION_WITHOUT_FINAL",
    "TELEGRAM_FINAL_DELIVERY_INVALID",
    "TELEGRAM_SESSION_RESUME_INVALID",
    "F4_MODEL_EFFORT_MISMATCH",
    "INVALID_PARAMETER_PROMPT_CACHE_RETENTION",
}


def _failure_status(code: str | None) -> StepStatus:
    return (
        StepStatus.INCOMPATIBLE
        if code in _DETERMINISTIC_INCOMPATIBILITY
        else StepStatus.ERROR
    )


def _fixture_incompatibility(config: OperationalConfig) -> bool:
    if not config.qualification_fixture or config.gate_fixture_counter_path is None:
        return False
    path = Path(config.gate_fixture_counter_path)
    invocation = 0
    if path.exists():
        try:
            value = json.loads(read_private_bytes(path, code="GATE_FIXTURE_COUNTER_INVALID"))
        except json.JSONDecodeError as exc:
            raise OperationalError("GATE_FIXTURE_COUNTER_INVALID") from exc
        if not isinstance(value, dict) or not isinstance(value.get("invocation"), int):
            raise OperationalError("GATE_FIXTURE_COUNTER_INVALID")
        invocation = value["invocation"]
    invocation += 1
    atomic_write_model(path, {"schema_version": 1, "invocation": invocation})
    return invocation <= config.gate_fixture_failures


def execute_check(request: CheckRequest, config: OperationalConfig) -> CompatibilityGateResult:
    candidate = request.candidate
    checks: dict[str, CheckEvidence] = {}
    try:
        verify_candidate(
            candidate, Path(config.manager_runtime_root) / "candidate-provenance.json"
        )
        _candidate_version(candidate, config)
        checks["candidate_version"] = _check("candidate_version", StepStatus.PASS)
    except OperationalError as exc:
        checks["candidate_version"] = _check(
            "candidate_version", _failure_status(exc.code), exc.code
        )
    try:
        _runtime_spec(candidate, config)
        checks["launcher_runtime_spec"] = _check("launcher_runtime_spec", StepStatus.PASS)
    except OperationalError as exc:
        checks["launcher_runtime_spec"] = _check(
            "launcher_runtime_spec", _failure_status(exc.code), exc.code
        )
    try:
        classification = _classification_checks(config)
    except OperationalError as exc:
        classification = {
            item: (False, exc.code)
            for item in (
                "reasoning_not_final",
                "tool_not_final",
                "system_not_final",
                "duplicate_final_suppressed",
                "completion_without_final_fails",
                "telegram_bot_import_origin",
                "rollout_schema_known",
            )
        }
    for check_id, (passed, failure) in classification.items():
        checks[check_id] = _check(
            check_id,
            StepStatus.PASS if passed else _failure_status(failure),
            None if passed else failure or "TELEGRAM_CLASSIFICATION_FAILED",
        )
    overlay_ok = tree_digest(Path(config.overlay_root)) == candidate.binding.combined_overlay_identity
    checks["combined_overlay_build"] = _check(
        "combined_overlay_build",
        StepStatus.PASS if overlay_ok else StepStatus.INCOMPATIBLE,
        None if overlay_ok else "OVERLAY_IDENTITY_MISMATCH",
    )
    manifest = Path(config.canonical_manifest_path)
    commit_manifest = manifest.with_name(f"{candidate.binding.telegram_commit}.json")
    contract_ok = (
        manifest.is_file()
        and commit_manifest.is_file()
        and file_sha256(manifest, code="CANONICAL_MANIFEST_UNSAFE")
        == file_sha256(commit_manifest, code="CANONICAL_MANIFEST_UNSAFE")
    )
    checks["canonical_compatibility_contract"] = _check(
        "canonical_compatibility_contract",
        StepStatus.PASS if contract_ok else StepStatus.INCOMPATIBLE,
        None if contract_ok else "CANONICAL_MANIFEST_MISMATCH",
    )
    try:
        real, _ = _real_canary(config, candidate)
    except OperationalError as exc:
        real = {
            item: (False, exc.code)
            for item in (
                "telegram_to_candidate_input",
                "candidate_to_telegram_final",
                "session_resume_history",
                "f4_runtime_truth",
            )
        }
    for check_id, (passed, failure) in real.items():
        checks[check_id] = _check(
            check_id,
            StepStatus.PASS if passed else _failure_status(failure),
            None if passed else failure or "REAL_CANDIDATE_CHECK_FAILED",
        )
    if _fixture_incompatibility(config):
        checks["canonical_compatibility_contract"] = _check(
            "canonical_compatibility_contract",
            StepStatus.INCOMPATIBLE,
            "QUALIFICATION_FIXTURE_INCOMPATIBLE",
        )
    ordered = [checks[item] for item in REQUIRED_GATE_CHECKS]
    failures = [item.failure_code or "CHECK_FAILED" for item in ordered if item.status != StepStatus.PASS]
    if any(item.status is StepStatus.ERROR for item in ordered):
        verdict = GateVerdict.CHECK_FAILED
    elif any(item.status is StepStatus.INCOMPATIBLE for item in ordered):
        verdict = GateVerdict.INCOMPATIBLE
    else:
        verdict = GateVerdict.COMPATIBLE
    return CompatibilityGateResult(
        verdict=verdict,
        binding=candidate.binding,
        checks=ordered,
        failure_codes=failures,
    )


def _handler(operation: str, request_path: Path, config: OperationalConfig) -> BaseModel:
    if operation != "check":
        raise OperationalError("OPERATION_UNSUPPORTED")
    request = read_private_model(request_path, CheckRequest, code="RUNNER_REQUEST_INVALID")
    return execute_check(request, config)


def main() -> None:
    raise SystemExit(protocol_main("compatibility-gate", _handler, sys.argv))


if __name__ == "__main__":
    main()
