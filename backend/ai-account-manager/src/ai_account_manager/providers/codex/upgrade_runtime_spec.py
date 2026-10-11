from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.core.launcher import build_codex_start_argv
from ai_account_manager.core.upgrade_manager import CandidateDescriptor
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    atomic_write_model,
    canonical_json,
    file_sha256,
    read_private_model,
    safe_executable,
)


class RuntimeProject(BaseModel):
    """The lifecycle state which reconciliation must preserve."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=160)
    project_path: str = Field(min_length=1, max_length=4096)
    lifecycle: Literal["running", "stopped"]


class CandidateRuntimeSpec(BaseModel):
    """Qualification form of the production Codex launch specification.

    ``candidate_argv`` is produced by the same launcher helper used by the
    backend API.  The document intentionally contains no auth material; the
    profile is an opaque identifier and CODEX_HOME remains runtime state.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    provider_id: Literal["codex"] = "codex"
    candidate_command: str
    candidate_argv: list[str] = Field(min_length=7, max_length=32)
    candidate_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    account_profile: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=80)
    projects: list[RuntimeProject] = Field(min_length=1, max_length=64)
    stopped_projects_started: Literal[False] = False
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _core(spec: CandidateRuntimeSpec) -> dict[str, object]:
    value = spec.model_dump(mode="json")
    value.pop("spec_sha256")
    return value


def calculate_spec_sha256(spec: CandidateRuntimeSpec) -> str:
    return hashlib.sha256(canonical_json(_core(spec))).hexdigest()


def _verify_runtime_spec_integrity(spec: CandidateRuntimeSpec) -> CandidateRuntimeSpec:
    """Verify stored facts without requiring derived argv to be current.

    Reconciliation exists specifically to replace stale launcher-derived argv.
    The source document must still be authentic, structurally safe, and bound
    to the exact immutable binary it names before its lifecycle is preserved.
    """

    if spec.spec_sha256 != calculate_spec_sha256(spec):
        raise OperationalError("RUNTIME_SPEC_STALE")
    if len({item.project_id for item in spec.projects}) != len(spec.projects):
        raise OperationalError("RUNTIME_SPEC_INVALID")
    for project in spec.projects:
        path = Path(project.project_path)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise OperationalError("RUNTIME_SPEC_INVALID")
    binary = safe_executable(Path(spec.candidate_command), code="RUNTIME_SPEC_BINARY_UNSAFE")
    if file_sha256(binary, code="RUNTIME_SPEC_BINARY_UNSAFE") != spec.candidate_digest:
        raise OperationalError("RUNTIME_SPEC_STALE")
    return spec


def verify_runtime_spec(spec: CandidateRuntimeSpec) -> CandidateRuntimeSpec:
    _verify_runtime_spec_integrity(spec)
    expected_argv = build_codex_start_argv(
        codex_binary=spec.candidate_command,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        cwd=spec.projects[0].project_path,
    )
    if spec.candidate_argv != expected_argv:
        raise OperationalError("RUNTIME_SPEC_STALE")
    return spec


def build_runtime_spec(
    *,
    candidate_command: Path,
    candidate_digest: str,
    candidate_version: str,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    projects: list[RuntimeProject],
) -> CandidateRuntimeSpec:
    binary = safe_executable(candidate_command, code="RUNTIME_SPEC_BINARY_UNSAFE")
    if file_sha256(binary, code="RUNTIME_SPEC_BINARY_UNSAFE") != candidate_digest:
        raise OperationalError("RUNTIME_SPEC_CANDIDATE_DIGEST_MISMATCH")
    if not projects:
        raise OperationalError("RUNTIME_SPEC_INVALID")
    argv = build_codex_start_argv(
        codex_binary=str(binary),
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=projects[0].project_path,
    )
    provisional = CandidateRuntimeSpec(
        candidate_command=str(binary),
        candidate_argv=argv,
        candidate_digest=candidate_digest,
        candidate_version=candidate_version,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        projects=projects,
        spec_sha256="0" * 64,
    )
    return provisional.model_copy(update={"spec_sha256": calculate_spec_sha256(provisional)})


def build_candidate_runtime_spec(
    candidate: CandidateDescriptor,
    *,
    account_profile: str,
    model: str,
    reasoning_effort: str,
    projects: list[RuntimeProject],
) -> CandidateRuntimeSpec:
    return build_runtime_spec(
        candidate_command=Path(candidate.binary_path),
        candidate_digest=candidate.binding.digest_sha256,
        candidate_version=candidate.binding.version,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        projects=projects,
    )


def read_runtime_spec(path: Path) -> CandidateRuntimeSpec:
    return verify_runtime_spec(
        read_private_model(path, CandidateRuntimeSpec, code="RUNTIME_SPEC_INVALID")
    )


def write_runtime_spec(path: Path, spec: CandidateRuntimeSpec) -> None:
    verify_runtime_spec(spec)
    atomic_write_model(path, spec)


def reconcile_runtime_spec(
    path: Path,
    candidate: CandidateDescriptor,
    *,
    account_profile: str,
    model: str,
    reasoning_effort: str,
) -> CandidateRuntimeSpec:
    """Validate stored facts, then regenerate derived argv with the current builder."""

    previous = _verify_runtime_spec_integrity(
        read_private_model(path, CandidateRuntimeSpec, code="RUNTIME_SPEC_INVALID")
    )
    stopped_before = {
        project.project_id for project in previous.projects if project.lifecycle == "stopped"
    }
    regenerated = build_candidate_runtime_spec(
        candidate,
        account_profile=account_profile,
        model=model,
        reasoning_effort=reasoning_effort,
        projects=[project.model_copy() for project in previous.projects],
    )
    stopped_after = {
        project.project_id for project in regenerated.projects if project.lifecycle == "stopped"
    }
    if stopped_after != stopped_before or regenerated.stopped_projects_started is not False:
        raise OperationalError("STOPPED_PROJECT_STARTED")
    write_runtime_spec(path, regenerated)
    return read_runtime_spec(path)
