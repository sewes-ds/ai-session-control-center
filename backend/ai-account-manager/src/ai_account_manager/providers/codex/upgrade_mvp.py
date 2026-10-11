from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ai_account_manager.core.upgrade_manager import (
    ActivationRunner,
    CandidateDescriptor,
    CompatibilityGate,
    CompatibilityGateResult,
    EngineeringResult,
    EngineeringSessionRunner,
    TransactionResult,
    UpgradeManagerError,
)
from ai_account_manager.providers.codex.upgrade_operational import RunnerFailure


MAX_RUNNER_OUTPUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
CODEX_GATE_VERSION = "codex-compatibility-gate-v1"
T = TypeVar("T", bound=BaseModel)
_VERSION_PATTERN = re.compile(r"^codex-cli (0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _safe_executable(path: Path, *, code: str) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise UpgradeManagerError(code)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise UpgradeManagerError(code) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise UpgradeManagerError(code)
    if metadata.st_uid not in {0, os.geteuid()} or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UpgradeManagerError(code)
    return resolved


def _candidate_digest(candidate: CandidateDescriptor) -> str:
    path = _safe_executable(Path(candidate.binary_path), code="candidate_binary_unsafe")
    digest = hashlib.sha256()
    before = path.lstat()
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UpgradeManagerError("candidate_observation_failed") from exc
    after = path.lstat()
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise UpgradeManagerError("candidate_changed_during_observation")
    value = digest.hexdigest()
    if value != candidate.binding.digest_sha256:
        raise UpgradeManagerError("candidate_digest_mismatch")
    return value


def _verify_candidate_version(candidate: CandidateDescriptor, timeout_seconds: float) -> None:
    binary = _safe_executable(Path(candidate.binary_path), code="candidate_binary_unsafe")
    with tempfile.TemporaryDirectory(prefix="cus-candidate-version-") as temporary_home:
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=min(timeout_seconds, 30.0),
                check=False,
                env={
                    "PATH": os.defpath,
                    "HOME": temporary_home,
                    "CODEX_HOME": temporary_home,
                    "LANG": "C",
                    "LC_ALL": "C",
                },
                start_new_session=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpgradeManagerError("candidate_version_check_failed") from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_RUNNER_OUTPUT_BYTES
        or len(completed.stderr) > MAX_RUNNER_OUTPUT_BYTES
    ):
        raise UpgradeManagerError("candidate_version_check_failed")
    try:
        output = completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise UpgradeManagerError("candidate_version_check_failed") from exc
    match = _VERSION_PATTERN.fullmatch(output)
    if match is None or ".".join(match.groups()) != candidate.binding.version:
        raise UpgradeManagerError("candidate_version_mismatch")


class JsonArtifactRunner:
    def __init__(
        self,
        executable: Path,
        *,
        timeout_seconds: float,
        config_path: Path | None = None,
    ) -> None:
        self.executable = executable
        if timeout_seconds <= 0 or timeout_seconds > 900:
            raise UpgradeManagerError("runner_timeout_invalid")
        self.timeout_seconds = timeout_seconds
        self.config_path = config_path

    def run(
        self,
        operation: str,
        payload: dict[str, object],
        result_model: type[T],
    ) -> T:
        executable = _safe_executable(self.executable, code="runner_unavailable")
        interpreter = Path(sys.executable)
        _safe_executable(interpreter, code="runner_interpreter_unavailable")
        with tempfile.TemporaryDirectory(prefix="cus-upgrade-runner-") as directory_name:
            directory = Path(directory_name)
            request_path = directory / "request.json"
            result_path = directory / "result.json"
            request_path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            request_path.chmod(0o600)
            environment = {
                "PATH": os.defpath,
                "HOME": directory_name,
                "LANG": "C",
                "LC_ALL": "C",
            }
            if self.config_path is not None:
                if not self.config_path.is_absolute() or ".." in self.config_path.parts:
                    raise UpgradeManagerError("runner_config_path_invalid")
                environment["CUS_CODEX_UPGRADE_CONFIG"] = str(self.config_path)
            try:
                completed = subprocess.run(
                    [
                        str(interpreter),
                        str(executable),
                        operation,
                        str(request_path),
                        str(result_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=environment,
                    start_new_session=True,
                    umask=0o077,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise UpgradeManagerError("runner_execution_failed") from exc
            if (
                len(completed.stdout) > MAX_RUNNER_OUTPUT_BYTES
                or len(completed.stderr) > MAX_RUNNER_OUTPUT_BYTES
            ):
                raise UpgradeManagerError("runner_output_too_large")
            if completed.returncode != 0:
                raise UpgradeManagerError("runner_execution_failed")
            if result_path.is_symlink() or not result_path.is_file():
                raise UpgradeManagerError("runner_result_missing")
            metadata = result_path.lstat()
            if (
                metadata.st_size > MAX_RESULT_BYTES
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise UpgradeManagerError("runner_result_unsafe")
            try:
                payload = result_path.read_bytes()
                return result_model.model_validate_json(payload)
            except OSError as exc:
                raise UpgradeManagerError("runner_result_invalid") from exc
            except (ValidationError, ValueError) as exc:
                try:
                    failure = RunnerFailure.model_validate_json(payload)
                except (ValidationError, ValueError):
                    raise UpgradeManagerError("runner_result_invalid") from exc
                raise UpgradeManagerError(failure.error_code) from exc


class CodexCompatibilityGate(CompatibilityGate):
    def __init__(self, runner: JsonArtifactRunner) -> None:
        self.runner = runner

    def run(self, candidate: CandidateDescriptor) -> CompatibilityGateResult:
        _candidate_digest(candidate)
        _verify_candidate_version(candidate, self.runner.timeout_seconds)
        return self.runner.run(
            "check",
            {
                "schema_version": 1,
                "candidate": candidate.model_dump(mode="json"),
            },
            CompatibilityGateResult,
        )


class CodexEngineeringSessionRunner(EngineeringSessionRunner):
    def __init__(self, runner: JsonArtifactRunner) -> None:
        self.runner = runner

    def start(
        self, candidate: CandidateDescriptor, evidence_path: Path, attempt: int
    ) -> EngineeringResult:
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise UpgradeManagerError("repair_evidence_unsafe")
        return self.runner.run(
            "start",
            {
                "schema_version": 1,
                "attempt": attempt,
                "candidate_binding": candidate.binding.model_dump(mode="json"),
                "evidence_path": str(evidence_path),
            },
            EngineeringResult,
        )

    def close(self, session_id: str) -> None:
        result = self.runner.run(
            "close",
            {"schema_version": 1, "session_id": session_id},
            _CloseResult,
        )
        if not result.closed:
            raise UpgradeManagerError("engineering_session_close_failed")


class _CloseResult(BaseModel):
    closed: bool


class CodexActivationRunner(ActivationRunner):
    def __init__(self, runner: JsonArtifactRunner) -> None:
        self.runner = runner

    def apply(self, candidate: CandidateDescriptor) -> TransactionResult:
        _candidate_digest(candidate)
        _verify_candidate_version(candidate, self.runner.timeout_seconds)
        return self.runner.run(
            "apply",
            {
                "schema_version": 1,
                "candidate": candidate.model_dump(mode="json"),
            },
            TransactionResult,
        )

    def rollback(self, candidate: CandidateDescriptor) -> TransactionResult:
        return self.runner.run(
            "rollback",
            {
                "schema_version": 1,
                "candidate_binding": candidate.binding.model_dump(mode="json"),
            },
            TransactionResult,
        )
