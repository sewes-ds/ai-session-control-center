from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.core.upgrade_manager import CandidateBinding
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    atomic_write_bytes,
    atomic_write_model,
    clean_environment,
    file_sha256,
    read_private_model,
    run_bounded,
    safe_executable,
)


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    fixture: bool
    attempt: int = Field(ge=1, le=5)
    approved_codex_binary: str
    approved_codex_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: str
    cus_worktree: str
    telegram_worktree: str
    evidence_path: str
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_path: str
    close_marker: str
    candidate_binding: CandidateBinding


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=cwd,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_AUTHOR_NAME": "CUS Qualification",
            "GIT_AUTHOR_EMAIL": "cus-qualification@invalid",
            "GIT_COMMITTER_NAME": "CUS Qualification",
            "GIT_COMMITTER_EMAIL": "cus-qualification@invalid",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=120,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise OperationalError("ENGINEERING_GIT_FAILED")
    return result.stdout.strip()


def execute(request: WorkerRequest) -> dict[str, object]:
    evidence = Path(request.evidence_path)
    if file_sha256(evidence, code="REPAIR_EVIDENCE_UNSAFE") != request.evidence_sha256:
        raise OperationalError("REPAIR_EVIDENCE_CHANGED")
    cus = Path(request.cus_worktree).resolve(strict=True)
    telegram = Path(request.telegram_worktree).resolve(strict=True)
    if _git(cus, "status", "--porcelain=v1") or _git(telegram, "status", "--porcelain=v1"):
        raise OperationalError("ENGINEERING_WORKTREE_DIRTY")
    binary = safe_executable(Path(request.approved_codex_binary), code="APPROVED_CODEX_BINARY_UNSAFE")
    if file_sha256(binary, code="APPROVED_CODEX_BINARY_UNSAFE") != request.approved_codex_sha256:
        raise OperationalError("APPROVED_CODEX_CHANGED")
    if request.fixture:
        codex = run_bounded(
            [str(binary), "--version"],
            cwd=cus,
            env=clean_environment(cus.parent / "worker-home"),
            timeout_seconds=30,
            maximum_output=16 * 1024,
        )
        marker = cus / "qualification" / f"repair-attempt-{request.attempt}.txt"
        marker.parent.mkdir(mode=0o700, exist_ok=True)
        marker.parent.chmod(0o700)
        atomic_write_bytes(
            marker,
            f"fixture attempt {request.attempt}; evidence {request.evidence_sha256}\n".encode(),
        )
    else:
        profile = Path(request.profile).resolve(strict=True)
        prompt = (
            "Work only in this isolated CUS clone. Review the evidence file at "
            f"{evidence} with SHA-256 {request.evidence_sha256}. Implement a minimal repair, "
            "do not deploy or push, and do not access credentials."
        )
        codex = run_bounded(
            [
                str(binary),
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "-C",
                str(cus),
                prompt,
            ],
            cwd=cus,
            env=clean_environment(profile, extra={"CODEX_HOME": str(profile)}),
            timeout_seconds=900,
        )
    if codex.returncode != 0:
        raise OperationalError("ENGINEERING_CODEX_FAILED")
    validation_exit_code = 0
    changed = _git(cus, "status", "--porcelain=v1").splitlines()
    if not changed:
        raise OperationalError("ENGINEERING_NO_CHANGE")
    changed_paths = [line[3:] for line in changed]
    forbidden_names = {
        ".env",
        "auth.json",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    }
    allowed_prefixes = (
        "backend/ai-account-manager/",
        "adapters/telegram-ai-agent/",
        "deploy/libexec/",
        "qualification/",
    )
    if any(
        path.startswith("/")
        or ".." in Path(path).parts
        or Path(path).name in forbidden_names
        or not path.startswith(allowed_prefixes)
        for path in changed_paths
    ):
        raise OperationalError("ENGINEERING_SCOPE_INVALID")
    if _git(telegram, "status", "--porcelain=v1"):
        raise OperationalError("ENGINEERING_SCOPE_INVALID")
    if not request.fixture:
        python_paths = [path for path in changed_paths if path.endswith(".py")]
        syntax_probe = (
            "import pathlib,sys; "
            "[compile(pathlib.Path(p).read_bytes(),p,'exec') for p in sys.argv[1:]]"
        )
        syntax = run_bounded(
            ["/usr/bin/python3", "-c", syntax_probe, *python_paths],
            cwd=cus,
            env=clean_environment(cus.parent / "validation-home"),
            timeout_seconds=120,
        )
        whitespace = run_bounded(
            ["/usr/bin/git", "diff", "--check"],
            cwd=cus,
            env=clean_environment(cus.parent / "validation-git-home"),
            timeout_seconds=120,
        )
        validation_exit_code = syntax.returncode or whitespace.returncode
        if validation_exit_code != 0:
            raise OperationalError("ENGINEERING_VALIDATION_FAILED")
    _git(cus, "add", "--", *changed_paths)
    _git(cus, "commit", "-m", f"test: qualification repair attempt {request.attempt}")
    cus_commit = _git(cus, "rev-parse", "HEAD")
    telegram_commit = _git(telegram, "rev-parse", "HEAD")
    return {
        "schema_version": 1,
        "completion_kind": "ENGINEERING_ATTESTATION",
        "codex_process_started": True,
        "worktree_clean_at_start": True,
        "changed_scope_validated": True,
        "cus_commit": cus_commit,
        "telegram_commit": telegram_commit,
        "combined_overlay_identity": request.candidate_binding.combined_overlay_identity,
        "approved_codex_sha256": request.approved_codex_sha256,
        "evidence_sha256": request.evidence_sha256,
        "changed_paths": changed_paths,
        "codex_exit_code": codex.returncode,
        "validation_exit_code": validation_exit_code,
    }


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    request = read_private_model(
        Path(sys.argv[1]), WorkerRequest, code="ENGINEERING_WORKER_REQUEST_INVALID"
    )
    try:
        atomic_write_model(Path(request.completion_path), execute(request))
    except OperationalError:
        return 2
    deadline = time.monotonic() + 3600
    while not Path(request.close_marker).exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
