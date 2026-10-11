from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    atomic_write_model,
    clean_environment,
    file_sha256,
    read_private_model,
    run_bounded,
    safe_executable,
)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_binary: str
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: str
    cwd: str
    final_marker: str
    started_path: str
    go_path: str


def _thread_id(output: bytes) -> str:
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            for key in ("thread_id", "session_id"):
                value = event.get(key)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", value):
                    return value
    raise OperationalError("REAL_CANARY_SESSION_ID_MISSING")


def _rollout(profile: Path, session_id: str) -> Path:
    matches = [
        path
        for path in (profile / "sessions").rglob("*.jsonl")
        if session_id in path.name and path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise OperationalError("REAL_CANARY_ROLLOUT_AMBIGUOUS")
    return matches[0]


def execute(request: Request) -> dict[str, object]:
    binary = safe_executable(Path(request.candidate_binary), code="CANDIDATE_BINARY_UNSAFE")
    if file_sha256(binary, code="CANDIDATE_BINARY_UNSAFE") != request.candidate_sha256:
        raise OperationalError("CANDIDATE_DIGEST_MISMATCH")
    profile = Path(request.profile).resolve(strict=True)
    cwd = Path(request.cwd).resolve(strict=True)
    atomic_write_model(Path(request.started_path), {"started": True})
    deadline = time.monotonic() + 30
    while not Path(request.go_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not Path(request.go_path).exists():
        raise OperationalError("REAL_CANARY_START_TIMEOUT")
    environment = clean_environment(profile, extra={"CODEX_HOME": str(profile)})
    prompt = (
        "Return exactly this marker as the final answer and nothing else: " + request.final_marker
    )
    first = run_bounded(
        [
            str(binary),
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-C",
            str(cwd),
            prompt,
        ],
        cwd=cwd,
        env=environment,
        timeout_seconds=480,
    )
    session_id = _thread_id(first.stdout)
    rollout = _rollout(profile, session_id)
    resume = run_bounded(
        [
            str(binary),
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-c",
            'sandbox_mode="read-only"',
            session_id,
            "Return the same exact marker again as final: " + request.final_marker,
        ],
        cwd=cwd,
        env=environment,
        timeout_seconds=480,
    )
    resumed_id = _thread_id(resume.stdout)
    return {
        "session_id": session_id,
        "resumed_session_id": resumed_id,
        "rollout_path": str(rollout),
        "first_exit_code": first.returncode,
        "resume_exit_code": resume.returncode,
    }


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    try:
        request = read_private_model(Path(sys.argv[1]), Request, code="REAL_CANARY_REQUEST_INVALID")
        atomic_write_model(Path(sys.argv[2]), execute(request))
        return 0
    except OperationalError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
