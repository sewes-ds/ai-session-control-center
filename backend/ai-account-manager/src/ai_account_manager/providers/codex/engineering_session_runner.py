from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.core.upgrade_manager import (
    CandidateBinding,
    CandidateDescriptor,
    EngineeringResult,
)
from ai_account_manager.providers.codex.upgrade_candidate import (
    profile_preflight,
    rebind_candidate_provenance,
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
    read_private_model,
    run_bounded,
    safe_executable,
)
from ai_account_manager.providers.codex.upgrade_overlay import build_overlay


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    attempt: int = Field(ge=1, le=5)
    candidate_binding: CandidateBinding
    evidence_path: str


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str = Field(pattern=r"^cus-eng-[0-9a-f]{16}$")


class CloseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed: bool


class WorkerCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    completion_kind: Literal["ENGINEERING_ATTESTATION"]
    codex_process_started: bool
    worktree_clean_at_start: bool
    changed_scope_validated: bool
    cus_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    telegram_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    combined_overlay_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_codex_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changed_paths: list[str] = Field(min_length=1, max_length=128)
    codex_exit_code: int
    validation_exit_code: int


class SessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    tmux_socket: str
    tmux_session_name: str
    session_root: str
    close_marker: str


def _git(
    config: OperationalConfig,
    cwd: Path,
    args: list[str],
    *,
    operation: str,
    evidence_class: Literal["FILESYSTEM_SIMULATION", "PROCESS_INTEGRATION"],
) -> bytes:
    command = ["/usr/bin/git", *args]
    result = run_bounded(
        command,
        cwd=cwd,
        env=clean_environment(Path(config.root) / "engineering-git-home"),
        timeout_seconds=120,
    )
    append_ledger(
        config,
        runner="engineering-session",
        operation=operation,
        argv=command,
        cwd=cwd,
        result=result,
        evidence_class=evidence_class,
        result_code="PASS" if result.returncode == 0 else "FAILED",
    )
    if result.returncode != 0:
        raise OperationalError("ENGINEERING_GIT_FAILED")
    return result.stdout


def _production_unchanged(config: OperationalConfig) -> bool:
    for repository, expected in (
        (Path(config.production_cus_repository), config.production_cus_commit),
        (Path(config.production_telegram_repository), config.production_telegram_commit),
    ):
        head = _git(
            config,
            repository,
            ["rev-parse", "HEAD"],
            operation="production-head-observe",
            evidence_class="PROCESS_INTEGRATION",
        ).decode("ascii", errors="strict").strip()
        status = _git(
            config,
            repository,
            ["status", "--porcelain=v1"],
            operation="production-status-observe",
            evidence_class="PROCESS_INTEGRATION",
        )
        if head != expected or status:
            return False
    return True


def _source_repository(
    config: OperationalConfig, source: Path, commit: str, label: str
) -> Path:
    def has_commit(repository: Path, operation: str) -> bool:
        command = ["/usr/bin/git", "cat-file", "-e", f"{commit}^{{commit}}"]
        result = run_bounded(
            command,
            cwd=repository,
            env=clean_environment(Path(config.root) / "engineering-git-home"),
            timeout_seconds=30,
            maximum_output=4096,
        )
        append_ledger(
            config,
            runner="engineering-session",
            operation=operation,
            argv=command,
            cwd=repository,
            result=result,
            evidence_class="PROCESS_INTEGRATION",
            result_code="FOUND" if result.returncode == 0 else "ABSENT",
        )
        return result.returncode == 0

    if has_commit(source, f"source-commit-{label}"):
        return source
    candidates: list[Path] = []
    for path in Path(config.engineering_root).glob(f"cus-eng-*/{label}"):
        if path.is_symlink() or not path.is_dir():
            continue
        if has_commit(path, f"retained-source-commit-{label}"):
            candidates.append(path)
    if len(candidates) != 1:
        raise OperationalError("ENGINEERING_SOURCE_COMMIT_MISSING")
    if _git(
        config,
        candidates[0],
        ["status", "--porcelain=v1"],
        operation=f"retained-source-clean-{label}",
        evidence_class="PROCESS_INTEGRATION",
    ):
        raise OperationalError("ENGINEERING_SOURCE_WORKTREE_DIRTY")
    return candidates[0]


def execute_start(request: StartRequest, config: OperationalConfig) -> EngineeringResult:
    binding = request.candidate_binding
    evidence = Path(request.evidence_path)
    expected_evidence = Path(config.manager_runtime_root) / f"repair-evidence-{request.attempt}.json"
    if evidence != expected_evidence or evidence.is_symlink():
        raise OperationalError("REPAIR_EVIDENCE_UNSAFE")
    evidence_digest = file_sha256(evidence, code="REPAIR_EVIDENCE_UNSAFE")
    if not _production_unchanged(config):
        raise OperationalError("PRODUCTION_CHANGED")
    if not config.engineering_fixture:
        profile = profile_preflight(
            Path(config.approved_codex_binary),
            Path(config.dedicated_test_profile),
            Path(config.root) / "engineering-profile-home",
        )
        if profile.status != "PROFILE_READY":
            raise OperationalError("DEDICATED_NON_PRODUCTION_CODEX_PROFILE")
    session_id = f"cus-eng-{secrets.token_hex(8)}"
    session_root = assert_private_directory(
        Path(config.engineering_root) / session_id,
        create=True,
        code="ENGINEERING_ROOT_UNSAFE",
    )
    cus_clone = session_root / "cus"
    telegram_clone = session_root / "telegram"
    for raw_source, destination, commit, label in (
        (Path(config.cus_repository), cus_clone, binding.cus_commit, "cus"),
        (Path(config.telegram_repository), telegram_clone, binding.telegram_commit, "telegram"),
    ):
        source = _source_repository(config, raw_source, commit, label)
        _git(
            config,
            session_root,
            ["clone", "--no-hardlinks", "--no-checkout", str(source), str(destination)],
            operation=f"clone-{label}",
            evidence_class="FILESYSTEM_SIMULATION",
        )
        _git(
            config,
            destination,
            ["checkout", "--detach", commit],
            operation=f"checkout-{label}",
            evidence_class="FILESYSTEM_SIMULATION",
        )
        if _git(
            config,
            destination,
            ["status", "--porcelain=v1"],
            operation=f"clean-start-{label}",
            evidence_class="FILESYSTEM_SIMULATION",
        ):
            raise OperationalError("ENGINEERING_WORKTREE_DIRTY")
    completion_path = session_root / "completion.json"
    close_marker = session_root / "close"
    worker_request = session_root / "worker-request.json"
    atomic_write_model(
        worker_request,
        {
            "schema_version": 1,
            "fixture": config.engineering_fixture,
            "attempt": request.attempt,
            "approved_codex_binary": config.approved_codex_binary,
            "approved_codex_sha256": file_sha256(
                Path(config.approved_codex_binary), code="APPROVED_CODEX_BINARY_UNSAFE"
            ),
            "profile": config.dedicated_test_profile,
            "cus_worktree": str(cus_clone),
            "telegram_worktree": str(telegram_clone),
            "evidence_path": str(evidence),
            "evidence_sha256": evidence_digest,
            "completion_path": str(completion_path),
            "close_marker": str(close_marker),
            "candidate_binding": binding.model_dump(mode="json"),
        },
    )
    socket = f"cus-eng-{session_id[-8:]}"
    tmux_name = session_id
    tmux = safe_executable(Path("/usr/bin/tmux"), code="TMUX_UNAVAILABLE")
    worker = safe_executable(
        Path(config.engineering_worker_executable), code="ENGINEERING_WORKER_UNSAFE"
    )
    command = [
        str(tmux),
        "-L",
        socket,
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-s",
        tmux_name,
        str(worker),
        str(worker_request),
    ]
    launched = run_bounded(
        command,
        cwd=session_root,
        env=clean_environment(session_root / "tmux-home"),
        timeout_seconds=30,
    )
    append_ledger(
        config,
        runner="engineering-session",
        operation="start-tmux",
        argv=command,
        cwd=session_root,
        result=launched,
        evidence_class="FILESYSTEM_SIMULATION" if config.engineering_fixture else "DISPOSABLE_E2E",
        result_code="STARTED" if launched.returncode == 0 else "FAILED",
    )
    if launched.returncode != 0:
        raise OperationalError("ENGINEERING_TMUX_FAILED")
    deadline = time.monotonic() + (180 if config.engineering_fixture else 900)
    while not completion_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not completion_path.exists():
        raise OperationalError("ENGINEERING_COMPLETION_TIMEOUT")
    completion = read_private_model(
        completion_path, WorkerCompletion, code="ENGINEERING_COMPLETION_INVALID"
    )
    records = assert_private_directory(
        Path(config.root) / "engineering-sessions",
        create=True,
        code="ENGINEERING_SESSION_RECORD_UNSAFE",
    )
    atomic_write_model(
        records / f"{session_id}.json",
        SessionRecord(
            session_id=session_id,
            tmux_socket=socket,
            tmux_session_name=tmux_name,
            session_root=str(session_root),
            close_marker=str(close_marker),
        ),
    )
    unchanged = _production_unchanged(config)
    if file_sha256(evidence, code="REPAIR_EVIDENCE_UNSAFE") != evidence_digest:
        unchanged = False
    if (
        completion.completion_kind != "ENGINEERING_ATTESTATION"
        or completion.approved_codex_sha256
        != file_sha256(Path(config.approved_codex_binary), code="APPROVED_CODEX_BINARY_UNSAFE")
        or completion.evidence_sha256 != evidence_digest
        or completion.codex_exit_code != 0
        or completion.validation_exit_code != 0
    ):
        raise OperationalError("ENGINEERING_COMPLETION_INVALID")
    if config.engineering_fixture:
        overlay_identity = completion.combined_overlay_identity
    else:
        overlay_config = config.model_copy(
            update={
                "cus_repository": str(cus_clone),
                "telegram_repository": str(telegram_clone),
            }
        )
        overlay_identity = build_overlay(
            overlay_config,
            expected_telegram_commit=completion.telegram_commit,
            replace_existing=True,
        ).identity
    updated_binding = binding.model_copy(
        update={
            "cus_commit": completion.cus_commit,
            "telegram_commit": completion.telegram_commit,
            "combined_overlay_identity": overlay_identity,
        }
    )
    candidate_path = Path(config.manager_runtime_root) / "candidate.json"
    candidate = read_private_model(
        candidate_path, CandidateDescriptor, code="CANDIDATE_DESCRIPTOR_INVALID"
    )
    rebound_candidate = candidate.model_copy(update={"binding": updated_binding})
    rebind_candidate_provenance(
        rebound_candidate,
        Path(config.manager_runtime_root) / "candidate-provenance.json",
    )
    return EngineeringResult(
        session_id=session_id,
        tmux_session_name=tmux_name,
        fresh_session=True,
        codex_process_started=completion.codex_process_started,
        worktree_clean_at_start=completion.worktree_clean_at_start,
        changed_scope_validated=completion.changed_scope_validated,
        production_unchanged=unchanged,
        evidence_sha256=evidence_digest,
        worktree_path=str(cus_clone),
        cus_commit=completion.cus_commit,
        telegram_commit=completion.telegram_commit,
        combined_overlay_identity=overlay_identity,
    )


def execute_close(request: CloseRequest, config: OperationalConfig) -> CloseResult:
    record_path = Path(config.root) / "engineering-sessions" / f"{request.session_id}.json"
    if not record_path.exists():
        return CloseResult(closed=True)
    record = read_private_model(record_path, SessionRecord, code="ENGINEERING_SESSION_RECORD_INVALID")
    if record.session_id != request.session_id:
        raise OperationalError("ENGINEERING_SESSION_MISMATCH")
    session_root = Path(record.session_root).resolve(strict=True)
    engineering_root = Path(config.engineering_root).resolve(strict=True)
    if not session_root.is_relative_to(engineering_root):
        raise OperationalError("ENGINEERING_SESSION_RECORD_INVALID")
    atomic_write_model(Path(record.close_marker), {"close": True})
    tmux = safe_executable(Path("/usr/bin/tmux"), code="TMUX_UNAVAILABLE")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        probe = run_bounded(
            [str(tmux), "-L", record.tmux_socket, "has-session", "-t", f"={record.tmux_session_name}"],
            cwd=Path(config.root),
            env=clean_environment(Path(config.root) / "tmux-close-home"),
            timeout_seconds=5,
            maximum_output=4096,
        )
        if probe.returncode != 0:
            break
        time.sleep(0.1)
    else:
        killed = run_bounded(
            [str(tmux), "-L", record.tmux_socket, "kill-session", "-t", f"={record.tmux_session_name}"],
            cwd=Path(config.root),
            env=clean_environment(Path(config.root) / "tmux-close-home"),
            timeout_seconds=10,
            maximum_output=4096,
        )
        if killed.returncode != 0:
            raise OperationalError("ENGINEERING_SESSION_CLOSE_FAILED")
    atomic_write_model(session_root / "closed.json", {"schema_version": 1, "closed": True})
    record_path.unlink()
    return CloseResult(closed=True)


def _handler(operation: str, request_path: Path, config: OperationalConfig) -> BaseModel:
    if operation == "start":
        return execute_start(
            read_private_model(request_path, StartRequest, code="RUNNER_REQUEST_INVALID"), config
        )
    if operation == "close":
        return execute_close(
            read_private_model(request_path, CloseRequest, code="RUNNER_REQUEST_INVALID"), config
        )
    raise OperationalError("OPERATION_UNSUPPORTED")


def main() -> None:
    raise SystemExit(protocol_main("engineering-session", _handler, sys.argv))


if __name__ == "__main__":
    main()
