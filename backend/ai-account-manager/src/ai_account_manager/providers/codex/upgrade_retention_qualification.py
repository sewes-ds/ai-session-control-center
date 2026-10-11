from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_account_manager.core.upgrade_retention import (
    POLICY_VERSION,
    CleanupCandidate,
    ReferenceSnapshot,
    RegisteredRoot,
    RetentionEngine,
    RetentionPolicy,
)
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalError,
    assert_private_directory,
    atomic_write_model,
    file_sha256,
)


TASK_ID = "TASK-CUS-CODEX-UPGRADE-RETENTION-008"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _evidence(digest: str, completed_at: str) -> dict[str, Any]:
    return {
        "candidate_version": "0.150.1",
        "candidate_sha256": digest,
        "compatibility_binding": {"provider_id": "codex", "digest_sha256": digest},
        "gate_statuses": [{"check_id": "candidate_version", "status": "PASS"}],
        "commands": [{"argv": ["retention-disposable-qualification"], "exit_code": 0}],
        "timestamps": {"completed_at": completed_at},
        "failure_or_block_reason": [],
        "test_summary": {"disposable": "PASSED"},
        "artifact_hashes": {"candidate": digest},
    }


def _tree(root: Path, name: str, size: int = 256 * 1024) -> Path:
    path = root / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    (path / "heavy.bin").write_bytes(b"x" * size)
    return path


def _candidate(
    path: Path,
    *,
    registration_id: str,
    root_id: str,
    digest: str,
    failed: bool = False,
    completed_at: str,
) -> CleanupCandidate:
    metadata = path.lstat()
    return CleanupCandidate(
        registration_id=registration_id,
        root_id=root_id,
        path=path,
        task_id=TASK_ID,
        job_id="retention-disposable-qualification",
        attempt_id=registration_id,
        kind="tree",
        completed_at=completed_at,
        failed=failed,
        expected_uid=metadata.st_uid,
        expected_gid=metadata.st_gid,
        expected_device=metadata.st_dev,
        implementation_commit="1" * 40,
        evidence_commit="2" * 40,
        compact_evidence=_evidence(digest, completed_at),
    )


def _root(root_id: str, path: Path) -> RegisteredRoot:
    metadata = path.lstat()
    return RegisteredRoot(
        root_id=root_id,
        path=path,
        expected_uid=metadata.st_uid,
        expected_gid=metadata.st_gid,
        expected_device=metadata.st_dev,
    )


def run(root: Path, result_path: Path) -> dict[str, Any]:
    if not root.is_absolute() or root == Path("/") or ".." in root.parts:
        raise OperationalError("RETENTION_QUALIFICATION_ROOT_UNSAFE")
    if root.exists() and any(root.iterdir()):
        raise OperationalError("RETENTION_QUALIFICATION_ROOT_NOT_EMPTY")
    root = assert_private_directory(root, create=True, code="RETENTION_QUALIFICATION_ROOT_UNSAFE")
    attempts = assert_private_directory(
        root / "attempts", create=True, code="RETENTION_QUALIFICATION_ROOT_UNSAFE"
    )
    releases = assert_private_directory(
        root / "releases", create=True, code="RETENTION_QUALIFICATION_ROOT_UNSAFE"
    )
    state = assert_private_directory(
        root / "state", create=True, code="RETENTION_QUALIFICATION_ROOT_UNSAFE"
    )
    shared = assert_private_directory(
        root / "codex-home", create=True, code="RETENTION_QUALIFICATION_ROOT_UNSAFE"
    )
    (shared / "auth-marker").write_text("shared-kept\n", encoding="utf-8")
    unrelated = root.parent / f"{root.name}-unrelated"
    unrelated.mkdir(mode=0o700)
    unrelated.chmod(0o700)
    (unrelated / "marker").write_text("untouched\n", encoding="utf-8")
    unrelated_before = file_sha256(unrelated / "marker", code="QUALIFICATION_MARKER_INVALID")

    now = datetime.now(UTC)
    clock = [now]
    engine = RetentionEngine(
        state_root=state,
        roots=(_root("attempts", attempts), _root("releases", releases)),
        usage_paths=(attempts, releases),
        filesystem_path=root,
        policy=RetentionPolicy(),
        now=lambda: clock[0],
        process_reference_probe=lambda _path: None,
        lock_probe=lambda _inodes: None,
    )
    usage_before = engine.disk_snapshot().disposable_allocated_bytes
    cycle_results: list[dict[str, Any]] = []
    maximum_heavy_attempts = 0
    candidate_digest = _digest("active-candidate")
    for number in range(1, 13):
        attempt_id = f"attempt-{number:02d}"
        target = _tree(attempts, attempt_id)
        engine.register(
            _candidate(
                target,
                registration_id=attempt_id,
                root_id="attempts",
                digest=candidate_digest,
                failed=number == 12,
                completed_at=clock[0].isoformat(),
            )
        )
        cycle = engine.sweep(
            operation_id=f"cycle-{number:02d}",
            references=ReferenceSnapshot(active_attempt_ids=(attempt_id,)),
            dry_run=False,
        )
        existing = sorted(path.name for path in attempts.iterdir() if path.is_dir())
        maximum_heavy_attempts = max(maximum_heavy_attempts, len(existing))
        if existing != [attempt_id]:
            raise OperationalError("RETENTION_ATTEMPT_GROWTH_UNBOUNDED")
        cycle_results.append(
            {
                "attempt_id": attempt_id,
                "cleanup_status": cycle.status,
                "heavy_attempts_remaining": len(existing),
            }
        )
        clock[0] = clock[0].replace(microsecond=0)

    active = _tree(releases, "0.150.1-active")
    previous = _tree(releases, "0.149.0-previous")
    stale = _tree(releases, "0.150.0-stale")
    for path, digest in (
        (active, candidate_digest),
        (previous, _digest("previous")),
        (stale, _digest("stale")),
    ):
        engine.register(
            _candidate(
                path,
                registration_id=path.name,
                root_id="releases",
                digest=digest,
                completed_at=clock[0].isoformat(),
            )
        )

    retry = _tree(attempts, "cleanup-retry")
    engine.register(
        _candidate(
            retry,
            registration_id="cleanup-retry",
            root_id="attempts",
            digest=_digest("retry"),
            completed_at=clock[0].isoformat(),
        )
    )
    delete_calls = [0]

    def fail_once(path: Path) -> None:
        if path == retry and delete_calls[0] == 0:
            delete_calls[0] += 1
            raise OSError("injected cleanup failure")
        shutil.rmtree(path)

    engine.delete_tree = fail_once
    keep = ReferenceSnapshot(
        keep_paths=(active, previous),
        active_attempt_ids=("attempt-12",),
    )
    retry_first = engine.sweep(
        operation_id="retry-first", references=keep, dry_run=False
    )
    retry_second = engine.sweep(
        operation_id="retry-second", references=keep, dry_run=False
    )
    engine.delete_tree = shutil.rmtree
    if retry_first.status != "FAILED" or retry_second.status not in {"PASSED", "PENDING"}:
        raise OperationalError("RETENTION_RETRY_NOT_IDEMPOTENT")
    if retry.exists():
        raise OperationalError("RETENTION_RETRY_TARGET_REMAINS")

    test_copy = _tree(attempts, "same-version-test-copy")
    engine.register(
        _candidate(
            test_copy,
            registration_id="same-version-test-copy",
            root_id="attempts",
            digest=candidate_digest,
            failed=True,
            completed_at=clock[0].isoformat(),
        )
    )
    final = engine.sweep(
        operation_id="verified-active-cleanup",
        references=ReferenceSnapshot(
            keep_paths=(active, previous, shared),
            active_candidate_digests=(candidate_digest,),
            completed_candidate_digests=(candidate_digest,),
        ),
        dry_run=False,
    )
    remaining_attempts = sorted(path.name for path in attempts.iterdir() if path.is_dir())
    remaining_releases = sorted(path.name for path in releases.iterdir() if path.is_dir())
    evidence_root = state / "compact-evidence"
    evidence = sorted(
        path
        for job_root in evidence_root.iterdir()
        if job_root.is_dir() and not job_root.is_symlink()
        for path in job_root.iterdir()
        if path.is_file() and not path.is_symlink() and path.suffix == ".json"
    )
    evidence_readable = all(
        json.loads(path.read_text(encoding="utf-8"))["policy_version"] == POLICY_VERSION
        for path in evidence
    )
    usage_after = engine.disk_snapshot().disposable_allocated_bytes
    unrelated_after = file_sha256(unrelated / "marker", code="QUALIFICATION_MARKER_INVALID")
    passed = all(
        (
            maximum_heavy_attempts <= 1,
            not remaining_attempts,
            remaining_releases == ["0.149.0-previous", "0.150.1-active"],
            shared.is_dir(),
            not test_copy.exists(),
            evidence_readable,
            usage_after < RetentionPolicy().hard_limit_bytes,
            unrelated_before == unrelated_after,
            final.status == "PASSED",
        )
    )
    result = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "policy_version": POLICY_VERSION,
        "status": "PASSED" if passed else "FAILED",
        "attempts_simulated": 12,
        "maximum_heavy_attempts": maximum_heavy_attempts,
        "cycle_results": cycle_results,
        "compact_evidence_count": len(evidence),
        "compact_evidence_readable": evidence_readable,
        "shared_codex_home_remains": shared.is_dir(),
        "active_remains": active.is_dir(),
        "previous_known_good_remains": previous.is_dir(),
        "same_version_test_copy_removed": not test_copy.exists(),
        "cleanup_retry_idempotent": not retry.exists(),
        "unrelated_untouched": unrelated_before == unrelated_after,
        "disposable_usage_before": usage_before,
        "disposable_usage_after": usage_after,
        "hard_limit_bytes": RetentionPolicy().hard_limit_bytes,
        "final_cleanup_status": final.status,
        "remaining_heavy_roots": len(remaining_attempts) + len(remaining_releases),
        "completed_at": clock[0].isoformat(),
    }
    if result["status"] != "PASSED":
        raise OperationalError("RETENTION_DISPOSABLE_QUALIFICATION_FAILED")
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result_path.parent.chmod(0o700)
    atomic_write_model(result_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.root, args.result)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
