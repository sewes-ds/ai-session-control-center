from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Sequence


GIB = 1024**3
POLICY_VERSION = "cus-upgrade-retention-v1"
MAX_JSON_BYTES = 4 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RetentionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RetentionPolicy:
    policy_version: str = POLICY_VERSION
    cleanup_trigger_bytes: int = 4 * GIB
    hard_limit_bytes: int = 6 * GIB
    minimum_free_bytes: int = 10 * GIB
    minimum_free_percent: int = 15
    latest_failed_ttl_seconds: int = 72 * 60 * 60
    compact_evidence_retention_seconds: int = 90 * 24 * 60 * 60
    retry_seconds: int = 60 * 60

    def __post_init__(self) -> None:
        if self.policy_version != POLICY_VERSION:
            raise RetentionError("RETENTION_POLICY_VERSION_INVALID")
        if not 0 < self.cleanup_trigger_bytes < self.hard_limit_bytes:
            raise RetentionError("RETENTION_POLICY_BUDGET_INVALID")
        if self.minimum_free_bytes <= 0 or not 1 <= self.minimum_free_percent <= 99:
            raise RetentionError("RETENTION_POLICY_FREE_SPACE_INVALID")
        if self.latest_failed_ttl_seconds != 72 * 60 * 60:
            raise RetentionError("RETENTION_POLICY_FAILED_TTL_INVALID")
        if self.compact_evidence_retention_seconds != 90 * 24 * 60 * 60:
            raise RetentionError("RETENTION_POLICY_EVIDENCE_TTL_INVALID")

    def required_free_bytes(self, filesystem_total_bytes: int) -> int:
        percent = (filesystem_total_bytes * self.minimum_free_percent + 99) // 100
        return max(self.minimum_free_bytes, percent)


@dataclass(frozen=True)
class RegisteredRoot:
    root_id: str
    path: Path
    expected_uid: int
    expected_gid: int | None = None
    expected_device: int | None = None

    def __post_init__(self) -> None:
        _validate_id(self.root_id, "REGISTERED_ROOT_ID_INVALID")
        if not self.path.is_absolute() or self.path == Path("/") or ".." in self.path.parts:
            raise RetentionError("REGISTERED_ROOT_PATH_INVALID")


@dataclass(frozen=True)
class CleanupCandidate:
    registration_id: str
    root_id: str
    path: Path
    task_id: str
    job_id: str
    attempt_id: str
    kind: Literal["tree", "git_worktree"]
    completed_at: str
    failed: bool
    expected_uid: int
    expected_gid: int
    expected_device: int
    expected_type: Literal["directory"] = "directory"
    repository_path: Path | None = None
    implementation_commit: str | None = None
    evidence_commit: str | None = None
    compact_evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, code in (
            (self.registration_id, "REGISTRATION_ID_INVALID"),
            (self.root_id, "REGISTERED_ROOT_ID_INVALID"),
            (self.task_id, "TASK_ID_INVALID"),
            (self.job_id, "JOB_ID_INVALID"),
            (self.attempt_id, "ATTEMPT_ID_INVALID"),
        ):
            _validate_id(value, code)
        if not self.path.is_absolute() or self.path == Path("/") or ".." in self.path.parts:
            raise RetentionError("CLEANUP_PATH_INVALID")
        if self.kind not in {"tree", "git_worktree"}:
            raise RetentionError("CLEANUP_KIND_INVALID")
        if self.expected_type != "directory":
            raise RetentionError("CLEANUP_EXPECTED_TYPE_INVALID")
        _parse_time(self.completed_at, "COMPLETED_AT_INVALID")
        if self.kind == "git_worktree" and self.repository_path is None:
            raise RetentionError("WORKTREE_REPOSITORY_REQUIRED")
        if self.repository_path is not None and (
            not self.repository_path.is_absolute()
            or self.repository_path == Path("/")
            or ".." in self.repository_path.parts
        ):
            raise RetentionError("WORKTREE_REPOSITORY_INVALID")
        for commit in (self.implementation_commit, self.evidence_commit):
            if commit is not None and not _COMMIT.fullmatch(commit):
                raise RetentionError("EVIDENCE_COMMIT_INVALID")


@dataclass(frozen=True)
class ReferenceSnapshot:
    keep_paths: tuple[Path, ...] = ()
    active_reference_paths: tuple[Path, ...] = ()
    active_tmux_paths: tuple[Path, ...] = ()
    active_job_ids: tuple[str, ...] = ()
    active_attempt_ids: tuple[str, ...] = ()
    active_candidate_digests: tuple[str, ...] = ()
    completed_candidate_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiskSnapshot:
    total_bytes: int
    free_bytes: int
    disposable_allocated_bytes: int


@dataclass(frozen=True)
class CleanupEntryResult:
    registration_id: str
    result: Literal["CLEANED", "SKIPPED", "FAILED", "WOULD_CLEAN"]
    reason: str
    allocated_bytes: int


@dataclass(frozen=True)
class CleanupRunResult:
    policy_version: str
    operation_id: str
    dry_run: bool
    status: Literal["PASSED", "PENDING", "FAILED", "DRY_RUN"]
    started_at: str
    completed_at: str
    reclaimed_bytes: int
    pending_cleanup_bytes: int
    cleaned_count: int
    skipped_count: int
    failed_count: int
    skipped_reasons: dict[str, int]
    entries: tuple[CleanupEntryResult, ...]
    disposable_usage_before: int
    disposable_usage_after: int
    next_retry_at: str | None

    def public_status(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "last_cleanup_at": self.completed_at,
            "last_cleanup_status": self.status,
            "reclaimed_bytes": self.reclaimed_bytes,
            "pending_cleanup_bytes": self.pending_cleanup_bytes,
            "cleaned_count": self.cleaned_count,
            "skipped_count": self.skipped_count,
            "skipped_reasons": dict(sorted(self.skipped_reasons.items())),
            "next_retry_at": self.next_retry_at,
        }


DeleteTree = Callable[[Path], None]
CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _validate_id(value: str, code: str) -> None:
    if not _SAFE_ID.fullmatch(value):
        raise RetentionError(code)


def _parse_time(value: str, code: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetentionError(code) from exc
    if parsed.tzinfo is None:
        raise RetentionError(code)
    return parsed.astimezone(UTC)


def _canonical_json(value: Any) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise RetentionError("RETENTION_JSON_INVALID") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise RetentionError("RETENTION_JSON_TOO_LARGE")
    return payload


def _assert_private_directory(path: Path, *, create: bool) -> Path:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise RetentionError("RETENTION_STATE_ROOT_UNSAFE")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RetentionError("RETENTION_STATE_ROOT_UNSAFE")
    if not path.exists():
        if not create:
            raise RetentionError("RETENTION_STATE_ROOT_MISSING")
        path.mkdir(mode=0o700, parents=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RetentionError("RETENTION_STATE_ROOT_UNSAFE")
    return path


def _atomic_write(path: Path, value: Any) -> str:
    parent = _assert_private_directory(path.parent, create=True)
    if path.is_symlink():
        raise RetentionError("RETENTION_STATE_FILE_UNSAFE")
    payload = _canonical_json(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> Any:
    if path.is_symlink():
        raise RetentionError("RETENTION_STATE_FILE_UNSAFE")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > MAX_JSON_BYTES
    ):
        raise RetentionError("RETENTION_STATE_FILE_UNSAFE")
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionError("RETENTION_STATE_FILE_INVALID") from exc


def _candidate_dict(candidate: CleanupCandidate) -> dict[str, Any]:
    value = asdict(candidate)
    value["path"] = str(candidate.path)
    value["repository_path"] = (
        str(candidate.repository_path) if candidate.repository_path is not None else None
    )
    return value


def _candidate_from_dict(value: Any) -> CleanupCandidate:
    if not isinstance(value, dict):
        raise RetentionError("REGISTRATION_INVALID")
    try:
        return CleanupCandidate(
            registration_id=str(value["registration_id"]),
            root_id=str(value["root_id"]),
            path=Path(str(value["path"])),
            task_id=str(value["task_id"]),
            job_id=str(value["job_id"]),
            attempt_id=str(value["attempt_id"]),
            kind=str(value["kind"]),  # type: ignore[arg-type]
            completed_at=str(value["completed_at"]),
            failed=bool(value["failed"]),
            expected_uid=int(value["expected_uid"]),
            expected_gid=int(value["expected_gid"]),
            expected_device=int(value["expected_device"]),
            expected_type=str(value.get("expected_type", "directory")),  # type: ignore[arg-type]
            repository_path=(
                Path(str(value["repository_path"])) if value.get("repository_path") else None
            ),
            implementation_commit=(
                str(value["implementation_commit"])
                if value.get("implementation_commit")
                else None
            ),
            evidence_commit=(
                str(value["evidence_commit"]) if value.get("evidence_commit") else None
            ),
            compact_evidence=dict(value.get("compact_evidence") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RetentionError("REGISTRATION_INVALID") from exc


def allocated_size(path: Path) -> tuple[int, set[tuple[int, int]]]:
    """Return allocated bytes without following symlinks or crossing filesystems."""

    try:
        root_metadata = path.lstat()
    except OSError as exc:
        raise RetentionError("ALLOCATED_SIZE_UNAVAILABLE") from exc
    root_device = root_metadata.st_dev
    total = 0
    seen: set[tuple[int, int]] = set()
    locked_inodes: set[tuple[int, int]] = set()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise RetentionError("ALLOCATED_SIZE_UNAVAILABLE") from exc
        if metadata.st_dev != root_device:
            raise RetentionError("FILESYSTEM_BOUNDARY_PRESENT")
        identity = (metadata.st_dev, metadata.st_ino)
        locked_inodes.add(identity)
        if identity in seen:
            continue
        seen.add(identity)
        total += metadata.st_blocks * 512
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            continue
        try:
            with os.scandir(current) as entries:
                children = [Path(entry.path) for entry in entries]
        except OSError as exc:
            raise RetentionError("ALLOCATED_SIZE_UNAVAILABLE") from exc
        stack.extend(sorted(children, reverse=True))
    return total, locked_inodes


def _is_within(reference: Path, target: Path) -> bool:
    try:
        reference.relative_to(target)
        return True
    except ValueError:
        return False


def _normalized_reference(raw: str) -> Path | None:
    value = raw.strip().removesuffix(" (deleted)")
    if not value.startswith("/"):
        return None
    return Path(value).resolve(strict=False)


def _transient_process_error(exc: OSError) -> bool:
    return exc.errno in {errno.ENOENT, errno.ESRCH, errno.ENOTDIR}


def process_reference_reason(
    target: Path,
    *,
    proc_root: Path = Path("/proc"),
) -> str | None:
    """Inspect process references and fail closed with a stable reason code."""

    try:
        processes = [item for item in proc_root.iterdir() if item.name.isdigit()]
    except PermissionError:
        return "INSPECTION_PERMISSION_UNAVAILABLE"
    except OSError:
        return "INSPECTION_RESULT_AMBIGUOUS"
    for proc in sorted(processes, key=lambda item: item.name):
        try:
            proc.lstat()
        except PermissionError:
            return "INSPECTION_PERMISSION_UNAVAILABLE"
        except OSError as exc:
            if _transient_process_error(exc):
                continue
            return "INSPECTION_RESULT_AMBIGUOUS"
        for name in ("cwd", "exe"):
            try:
                raw = os.readlink(proc / name)
            except PermissionError:
                return "INSPECTION_PERMISSION_UNAVAILABLE"
            except OSError as exc:
                if _transient_process_error(exc):
                    continue
                return "INSPECTION_RESULT_AMBIGUOUS"
            reference = _normalized_reference(raw)
            if reference is not None and _is_within(reference, target):
                return f"PROCESS_{name.upper()}_REFERENCE"
        fd_root = proc / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except PermissionError:
            return "INSPECTION_PERMISSION_UNAVAILABLE"
        except OSError as exc:
            if _transient_process_error(exc):
                continue
            return "INSPECTION_RESULT_AMBIGUOUS"
        for descriptor in descriptors:
            try:
                raw = os.readlink(descriptor)
            except PermissionError:
                return "INSPECTION_PERMISSION_UNAVAILABLE"
            except OSError as exc:
                if _transient_process_error(exc):
                    continue
                return "INSPECTION_RESULT_AMBIGUOUS"
            reference = _normalized_reference(raw)
            if reference is not None and _is_within(reference, target):
                return "PROCESS_OPEN_FD_REFERENCE"
        try:
            maps = (proc / "maps").read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            return "INSPECTION_PERMISSION_UNAVAILABLE"
        except OSError as exc:
            if _transient_process_error(exc):
                continue
            return "INSPECTION_RESULT_AMBIGUOUS"
        for line in maps.splitlines():
            raw = line.split(maxsplit=5)[-1] if len(line.split(maxsplit=5)) == 6 else ""
            reference = _normalized_reference(raw)
            if reference is not None and _is_within(reference, target):
                return "PROCESS_MAP_REFERENCE"
    return None


# Backward-compatible private name for callers that used the engine default.
_process_reference_reason = process_reference_reason


def _held_lock_reason(inodes: set[tuple[int, int]]) -> str | None:
    try:
        lines = Path("/proc/locks").read_text(encoding="ascii", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError):
        return "LOCK_SCAN_UNCERTAIN"
    inode_numbers = {inode for _device, inode in inodes}
    for line in lines:
        fields = line.split()
        if len(fields) < 6:
            continue
        raw = fields[5]
        try:
            inode = int(raw.rsplit(":", 1)[-1])
        except ValueError:
            continue
        if inode in inode_numbers:
            return "HELD_LOCK_REFERENCE"
    return None


def _default_remove_tree(path: Path) -> None:
    root = path.resolve(strict=True)
    root_metadata = root.lstat()

    def onexc(function: Callable[..., Any], failed: str, _exc: BaseException) -> None:
        failed_path = Path(failed)
        repairs = [failed_path]
        if failed_path != root and _is_within(failed_path.parent, root):
            repairs.append(failed_path.parent)
        try:
            for repair in repairs:
                metadata = repair.lstat()
                if (
                    metadata.st_uid != root_metadata.st_uid
                    or metadata.st_gid != root_metadata.st_gid
                    or metadata.st_dev != root_metadata.st_dev
                    or (repair != root and not _is_within(repair, root))
                ):
                    raise OSError("unsafe cleanup permission repair")
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                repair.chmod(
                    0o700 if stat.S_ISDIR(metadata.st_mode) else 0o600,
                    follow_symlinks=False,
                )
        except (OSError, NotImplementedError):
            raise
        function(failed)

    shutil.rmtree(path, onexc=onexc)


class RetentionEngine:
    def __init__(
        self,
        *,
        state_root: Path,
        roots: Sequence[RegisteredRoot],
        usage_paths: Sequence[Path],
        filesystem_path: Path,
        policy: RetentionPolicy | None = None,
        now: Callable[[], datetime] | None = None,
        delete_tree: DeleteTree = _default_remove_tree,
        command_runner: CommandRunner = subprocess.run,
        process_reference_probe: Callable[[Path], str | None] = _process_reference_reason,
        lock_probe: Callable[[set[tuple[int, int]]], str | None] = _held_lock_reason,
    ) -> None:
        self.state_root = state_root
        self.roots = {item.root_id: item for item in roots}
        if len(self.roots) != len(roots):
            raise RetentionError("REGISTERED_ROOT_DUPLICATE")
        self.usage_paths = tuple(usage_paths)
        self.filesystem_path = filesystem_path
        self.policy = policy or RetentionPolicy()
        self.now = now or (lambda: datetime.now(UTC))
        self.delete_tree = delete_tree
        self.command_runner = command_runner
        self.process_reference_probe = process_reference_probe
        self.lock_probe = lock_probe

    @property
    def registry_root(self) -> Path:
        return self.state_root / "registrations"

    @property
    def evidence_root(self) -> Path:
        return self.state_root / "compact-evidence"

    @property
    def manifest_root(self) -> Path:
        return self.state_root / "manifests"

    @property
    def result_root(self) -> Path:
        return self.state_root / "results"

    @property
    def status_path(self) -> Path:
        return self.state_root / "status.json"

    @contextmanager
    def operation_lock(self) -> Iterator[None]:
        root = _assert_private_directory(self.state_root, create=True)
        path = root / ".cleanup.lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RetentionError("CLEANUP_LOCK_UNSAFE") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise RetentionError("CLEANUP_LOCK_UNSAFE")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RetentionError("CLEANUP_IN_PROGRESS") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _root_for(self, candidate: CleanupCandidate) -> RegisteredRoot:
        root = self.roots.get(candidate.root_id)
        if root is None:
            raise RetentionError("CLEANUP_ROOT_NOT_REGISTERED")
        return root

    def _validate_exact_path(
        self, candidate: CleanupCandidate
    ) -> tuple[Path, int, set[tuple[int, int]]]:
        root = self._root_for(candidate)
        current = Path(root.path.anchor)
        for part in root.path.parts[1:]:
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise RetentionError("CLEANUP_ROOT_SYMLINK")
        root_real = root.path.resolve(strict=True)
        root_metadata = root_real.lstat()
        expected_device = root.expected_device or root_metadata.st_dev
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != root.expected_uid
            or (
                root.expected_gid is not None
                and root_metadata.st_gid != root.expected_gid
            )
            or root_metadata.st_dev != expected_device
        ):
            raise RetentionError("CLEANUP_ROOT_IDENTITY_MISMATCH")
        current = Path(candidate.path.anchor)
        for part in candidate.path.parts[1:]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise RetentionError("CLEANUP_PATH_SYMLINK")
        target = candidate.path.resolve(strict=True)
        if target == root_real or not _is_within(target, root_real):
            raise RetentionError("CLEANUP_PATH_ESCAPE")
        metadata = target.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != candidate.expected_uid
            or metadata.st_gid != candidate.expected_gid
            or metadata.st_dev != candidate.expected_device
            or metadata.st_dev != expected_device
        ):
            raise RetentionError("CLEANUP_TARGET_IDENTITY_MISMATCH")
        allocated, inodes = allocated_size(target)
        return target, allocated, inodes

    def register(self, candidate: CleanupCandidate) -> Path:
        self._validate_exact_path(candidate)
        _assert_private_directory(self.registry_root, create=True)
        path = self.registry_root / f"{candidate.registration_id}.json"
        if path.exists():
            existing = _candidate_from_dict(_read_json(path))
            if existing != candidate:
                raise RetentionError("REGISTRATION_ID_REUSED")
            return path
        _atomic_write(path, _candidate_dict(candidate))
        return path

    def registrations(self) -> list[CleanupCandidate]:
        if not self.registry_root.exists():
            return []
        _assert_private_directory(self.registry_root, create=False)
        values: list[CleanupCandidate] = []
        for path in sorted(self.registry_root.iterdir(), key=lambda item: item.name):
            if path.name.endswith(".deleted.json"):
                continue
            if not path.name.endswith(".json") or not _SAFE_ID.fullmatch(path.stem):
                raise RetentionError("REGISTRY_ENTRY_UNSAFE")
            values.append(_candidate_from_dict(_read_json(path)))
        return values

    def disk_snapshot(self) -> DiskSnapshot:
        try:
            filesystem = os.statvfs(self.filesystem_path)
        except OSError as exc:
            raise RetentionError("DISK_SPACE_UNAVAILABLE") from exc
        total = filesystem.f_blocks * filesystem.f_frsize
        free = filesystem.f_bavail * filesystem.f_frsize
        usage = 0
        identities: set[tuple[int, int]] = set()
        for path in self.usage_paths:
            if not path.exists():
                continue
            value, inodes = allocated_size(path)
            if identities.intersection(inodes):
                # Overlapping usage manifests are ambiguous and must not be double-counted.
                raise RetentionError("DISPOSABLE_USAGE_MANIFEST_OVERLAP")
            identities.update(inodes)
            usage += value
        return DiskSnapshot(total_bytes=total, free_bytes=free, disposable_allocated_bytes=usage)

    def _evidence_payload(
        self,
        candidate: CleanupCandidate,
        *,
        manifest: dict[str, Any],
        planned_result: str,
    ) -> dict[str, Any]:
        evidence = dict(candidate.compact_evidence)
        evidence.update(
            {
                "policy_version": self.policy.policy_version,
                "task_id": candidate.task_id,
                "job_id": candidate.job_id,
                "attempt_id": candidate.attempt_id,
                "cleanup_manifest": manifest,
                "cleanup_result": planned_result,
                "implementation_commit": candidate.implementation_commit,
                "evidence_commit": candidate.evidence_commit,
            }
        )
        required = {
            "policy_version",
            "task_id",
            "job_id",
            "attempt_id",
            "candidate_version",
            "candidate_sha256",
            "compatibility_binding",
            "gate_statuses",
            "commands",
            "timestamps",
            "failure_or_block_reason",
            "test_summary",
            "artifact_hashes",
            "cleanup_manifest",
            "cleanup_result",
            "implementation_commit",
            "evidence_commit",
        }
        if set(evidence) < required:
            raise RetentionError("COMPACT_EVIDENCE_INCOMPLETE")
        for key in ("implementation_commit", "evidence_commit"):
            if not isinstance(evidence[key], str) or not _COMMIT.fullmatch(evidence[key]):
                raise RetentionError("COMPACT_EVIDENCE_COMMIT_INVALID")
        if not isinstance(evidence["commands"], list) or any(
            not isinstance(item, dict) or not isinstance(item.get("exit_code"), int)
            for item in evidence["commands"]
        ):
            raise RetentionError("COMPACT_EVIDENCE_COMMANDS_INVALID")
        if not _SHA256.fullmatch(str(evidence["candidate_sha256"])):
            raise RetentionError("COMPACT_EVIDENCE_CANDIDATE_INVALID")
        return evidence

    def _persist_evidence(
        self, candidate: CleanupCandidate, manifest: dict[str, Any], planned_result: str
    ) -> tuple[Path, str]:
        _assert_private_directory(self.evidence_root, create=True)
        job_root = self.evidence_root / candidate.job_id
        _assert_private_directory(job_root, create=True)
        path = job_root / f"{candidate.attempt_id}.json"
        payload = self._evidence_payload(
            candidate, manifest=manifest, planned_result=planned_result
        )
        digest = _atomic_write(path, payload)
        observed = _read_json(path)
        if _canonical_json(observed) != _canonical_json(payload):
            raise RetentionError("COMPACT_EVIDENCE_VERIFICATION_FAILED")
        if hashlib.sha256(_canonical_json(observed)).hexdigest() != digest:
            raise RetentionError("COMPACT_EVIDENCE_VERIFICATION_FAILED")
        return path, digest

    def _reference_reason(
        self,
        candidate: CleanupCandidate,
        target: Path,
        references: ReferenceSnapshot,
    ) -> str | None:
        for keep in references.keep_paths:
            if keep.resolve(strict=False) == target or _is_within(keep.resolve(strict=False), target):
                return "KEEP_SET"
        for reference in references.active_reference_paths:
            if _is_within(reference.resolve(strict=False), target):
                return "ACTIVE_RUNTIME_REFERENCE"
        for reference in references.active_tmux_paths:
            if _is_within(reference.resolve(strict=False), target):
                return "ACTIVE_TMUX_REFERENCE"
        if candidate.job_id in references.active_job_ids:
            return "ACTIVE_JOB_REFERENCE"
        if candidate.attempt_id in references.active_attempt_ids:
            return "ACTIVE_ATTEMPT_REFERENCE"
        digest = str(candidate.compact_evidence.get("candidate_sha256") or "")
        if (
            candidate.root_id == "production-releases"
            and digest
            and digest in references.active_candidate_digests
        ):
            return "ACTIVE_CANDIDATE_REFERENCE"
        return None

    def _worktree_guard(self, candidate: CleanupCandidate, target: Path) -> str | None:
        if candidate.kind != "git_worktree":
            return None
        repository = candidate.repository_path
        if repository is None:
            return "WORKTREE_REPOSITORY_REQUIRED"
        commands = (
            ["/usr/bin/git", "-C", str(target), "status", "--porcelain=v1"],
            ["/usr/bin/git", "-C", str(target), "rev-parse", "HEAD"],
            ["/usr/bin/git", "-C", str(repository), "worktree", "list", "--porcelain"],
        )
        outputs: list[bytes] = []
        for command in commands:
            try:
                result = self.command_runner(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired):
                return "WORKTREE_GIT_UNCERTAIN"
            if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 1024 * 1024:
                return "WORKTREE_GIT_UNCERTAIN"
            outputs.append(result.stdout)
        if outputs[0].strip():
            return "WORKTREE_DIRTY"
        try:
            head = outputs[1].decode("ascii", errors="strict").strip()
            inventory = outputs[2].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "WORKTREE_GIT_UNCERTAIN"
        if not _COMMIT.fullmatch(head) or f"worktree {target}\n" not in inventory:
            return "WORKTREE_NOT_REGISTERED"
        for commit in (head, candidate.implementation_commit, candidate.evidence_commit):
            if commit is None:
                return "WORKTREE_COMMIT_EVIDENCE_MISSING"
            try:
                result = self.command_runner(
                    [
                        "/usr/bin/git",
                        "-C",
                        str(repository),
                        "for-each-ref",
                        "--format=%(refname)",
                        "--contains",
                        commit,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired):
                return "WORKTREE_COMMIT_UNCERTAIN"
            if result.returncode != 0 or not result.stdout.strip():
                return "WORKTREE_COMMIT_UNREACHABLE"
        return None

    def _remove(self, candidate: CleanupCandidate, target: Path) -> None:
        if candidate.kind == "tree":
            self.delete_tree(target)
            return
        repository = candidate.repository_path
        if repository is None:
            raise RetentionError("WORKTREE_REPOSITORY_REQUIRED")
        for command in (
            ["/usr/bin/git", "-C", str(repository), "worktree", "remove", str(target)],
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "worktree",
                "prune",
                "--expire",
                "now",
            ],
        ):
            try:
                result = self.command_runner(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=60,
                    check=False,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RetentionError("WORKTREE_REMOVE_FAILED") from exc
            if result.returncode != 0:
                raise RetentionError("WORKTREE_REMOVE_FAILED")

    def _superseded_candidates(
        self,
        registrations: Sequence[CleanupCandidate],
        references: ReferenceSnapshot,
    ) -> list[CleanupCandidate]:
        now = self.now()
        failed = sorted(
            (item for item in registrations if item.failed),
            key=lambda item: (_parse_time(item.completed_at, "COMPLETED_AT_INVALID"), item.registration_id),
            reverse=True,
        )
        latest_failed = failed[0] if failed else None
        selected: list[CleanupCandidate] = []
        for item in sorted(
            registrations,
            key=lambda value: (
                _parse_time(value.completed_at, "COMPLETED_AT_INVALID"),
                value.registration_id,
            ),
        ):
            if item.job_id in references.active_job_ids or item.attempt_id in references.active_attempt_ids:
                continue
            digest = str(item.compact_evidence.get("candidate_sha256") or "")
            if (
                item is latest_failed
                and digest not in references.completed_candidate_digests
                and now
                < _parse_time(item.completed_at, "COMPLETED_AT_INVALID")
                + timedelta(seconds=self.policy.latest_failed_ttl_seconds)
            ):
                continue
            selected.append(item)
        return selected

    def sweep(
        self,
        *,
        operation_id: str,
        references: ReferenceSnapshot,
        dry_run: bool,
        historical_paths: Sequence[Path] = (),
    ) -> CleanupRunResult:
        _validate_id(operation_id, "CLEANUP_OPERATION_ID_INVALID")
        with self.operation_lock():
            started = self.now()
            before = self.disk_snapshot()
            registrations = self.registrations()
            candidates = self._superseded_candidates(registrations, references)
            entries: list[CleanupEntryResult] = []
            reclaimed = 0
            pending = 0
            cleaned = 0
            skipped = 0
            failed = 0
            retry_required = False
            reasons: dict[str, int] = {}
            manifest_entries: list[dict[str, Any]] = []
            for path in sorted(historical_paths, key=str):
                size = allocated_size(path)[0] if path.exists() else 0
                entries.append(
                    CleanupEntryResult(
                        registration_id="historical-dry-run",
                        result="SKIPPED",
                        reason="HISTORICAL_DRY_RUN_ONLY",
                        allocated_bytes=size,
                    )
                )
                skipped += 1
                pending += size
                retry_required = True
                reasons["HISTORICAL_DRY_RUN_ONLY"] = reasons.get("HISTORICAL_DRY_RUN_ONLY", 0) + 1
            for candidate in candidates:
                try:
                    target, allocated, inodes = self._validate_exact_path(candidate)
                except FileNotFoundError:
                    self._mark_deleted(candidate, "ALREADY_ABSENT", None, None)
                    continue
                except RetentionError as exc:
                    allocated = 0
                    reason = exc.code
                    entries.append(
                        CleanupEntryResult(candidate.registration_id, "SKIPPED", reason, allocated)
                    )
                    skipped += 1
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                reason = self._reference_reason(candidate, target, references)
                reason = reason or self.process_reference_probe(target)
                reason = reason or self.lock_probe(inodes)
                reason = reason or self._worktree_guard(candidate, target)
                manifest_entry = {
                    "registration_id": candidate.registration_id,
                    "root_id": candidate.root_id,
                    "path_sha256": hashlib.sha256(str(target).encode("utf-8")).hexdigest(),
                    "expected_uid": candidate.expected_uid,
                    "expected_gid": candidate.expected_gid,
                    "expected_device": candidate.expected_device,
                    "expected_type": candidate.expected_type,
                    "allocated_bytes": allocated,
                    "guards": "PASS" if reason is None else reason,
                }
                manifest_entries.append(manifest_entry)
                if reason is not None:
                    entries.append(
                        CleanupEntryResult(candidate.registration_id, "SKIPPED", reason, allocated)
                    )
                    skipped += 1
                    if reason not in {"KEEP_SET", "ACTIVE_RUNTIME_REFERENCE"}:
                        pending += allocated
                        retry_required = True
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                manifest = {
                    "policy_version": self.policy.policy_version,
                    "operation_id": operation_id,
                    "exact_paths_only": True,
                    "entry": manifest_entry,
                }
                try:
                    evidence_path, evidence_sha = self._persist_evidence(
                        candidate,
                        manifest,
                        "WOULD_CLEAN" if dry_run else "PENDING",
                    )
                except RetentionError as exc:
                    reason = exc.code
                    entries.append(
                        CleanupEntryResult(candidate.registration_id, "SKIPPED", reason, allocated)
                    )
                    skipped += 1
                    pending += allocated
                    retry_required = True
                    reasons[reason] = reasons.get(reason, 0) + 1
                    continue
                if dry_run:
                    entries.append(
                        CleanupEntryResult(candidate.registration_id, "WOULD_CLEAN", "GUARDS_PASS", allocated)
                    )
                    pending += allocated
                    continue
                try:
                    self._remove(candidate, target)
                    if target.exists() or target.is_symlink():
                        raise RetentionError("CLEANUP_TARGET_REMAINS")
                    evidence_path, evidence_sha = self._persist_evidence(
                        candidate, manifest, "CLEANED"
                    )
                    self._mark_deleted(candidate, "CLEANED", evidence_path, evidence_sha)
                except (OSError, RetentionError):
                    entries.append(
                        CleanupEntryResult(candidate.registration_id, "FAILED", "DELETE_FAILED", allocated)
                    )
                    failed += 1
                    retry_required = True
                    pending += allocated_size(target)[0] if target.exists() else 0
                    reasons["DELETE_FAILED"] = reasons.get("DELETE_FAILED", 0) + 1
                    continue
                entries.append(
                    CleanupEntryResult(candidate.registration_id, "CLEANED", "GUARDS_PASS", allocated)
                )
                cleaned += 1
                reclaimed += allocated
            (
                evidence_entries,
                evidence_cleaned,
                evidence_skipped,
                evidence_reclaimed,
                evidence_reasons,
            ) = self._prune_expired_evidence(dry_run=dry_run)
            entries.extend(evidence_entries)
            cleaned += evidence_cleaned
            skipped += evidence_skipped
            reclaimed += evidence_reclaimed
            if evidence_skipped:
                retry_required = True
            for reason, count in evidence_reasons.items():
                reasons[reason] = reasons.get(reason, 0) + count
            completed = self.now()
            after = self.disk_snapshot()
            if dry_run:
                status: Literal["PASSED", "PENDING", "FAILED", "DRY_RUN"] = "DRY_RUN"
            elif failed:
                status = "FAILED"
            elif retry_required:
                status = "PENDING"
            else:
                status = "PASSED"
            retry = (
                (completed + timedelta(seconds=self.policy.retry_seconds)).isoformat()
                if status in {"PENDING", "FAILED"}
                else None
            )
            result = CleanupRunResult(
                policy_version=self.policy.policy_version,
                operation_id=operation_id,
                dry_run=dry_run,
                status=status,
                started_at=started.isoformat(),
                completed_at=completed.isoformat(),
                reclaimed_bytes=reclaimed,
                pending_cleanup_bytes=pending,
                cleaned_count=cleaned,
                skipped_count=skipped,
                failed_count=failed,
                skipped_reasons=reasons,
                entries=tuple(entries),
                disposable_usage_before=before.disposable_allocated_bytes,
                disposable_usage_after=after.disposable_allocated_bytes,
                next_retry_at=retry,
            )
            _assert_private_directory(self.manifest_root, create=True)
            _assert_private_directory(self.result_root, create=True)
            _atomic_write(
                self.manifest_root / f"{operation_id}.json",
                {
                    "policy_version": self.policy.policy_version,
                    "operation_id": operation_id,
                    "dry_run": dry_run,
                    "entries": manifest_entries,
                    "historical_paths_count": len(historical_paths),
                },
            )
            _atomic_write(self.result_root / f"{operation_id}.json", asdict(result))
            _atomic_write(self.status_path, result.public_status())
            return result

    def _mark_deleted(
        self,
        candidate: CleanupCandidate,
        result: str,
        evidence_path: Path | None,
        evidence_sha256: str | None,
    ) -> None:
        source = self.registry_root / f"{candidate.registration_id}.json"
        target = self.registry_root / f"{candidate.registration_id}.deleted.json"
        payload = _candidate_dict(candidate)
        payload.update(
            {
                "cleanup_result": result,
                "cleanup_completed_at": self.now().isoformat(),
                "compact_evidence_path": str(evidence_path) if evidence_path else None,
                "compact_evidence_sha256": evidence_sha256,
            }
        )
        _atomic_write(target, payload)
        source.unlink(missing_ok=True)

    def _prune_expired_evidence(
        self,
        *,
        dry_run: bool,
    ) -> tuple[list[CleanupEntryResult], int, int, int, dict[str, int]]:
        if not self.registry_root.exists():
            return [], 0, 0, 0, {}
        entries: list[CleanupEntryResult] = []
        cleaned = 0
        skipped = 0
        reclaimed = 0
        reasons: dict[str, int] = {}
        evidence_root = self.evidence_root.resolve(strict=False)
        deleted_registrations = [
            item
            for item in self.registry_root.iterdir()
            if item.name.endswith(".deleted.json")
        ]
        for registration in sorted(deleted_registrations, key=lambda item: item.name):
            raw = _read_json(registration)
            if not isinstance(raw, dict) or raw.get("compact_evidence_expired_at"):
                continue
            candidate = _candidate_from_dict(raw)
            expires = _parse_time(
                candidate.completed_at, "COMPLETED_AT_INVALID"
            ) + timedelta(seconds=self.policy.compact_evidence_retention_seconds)
            if self.now() < expires:
                continue
            value = raw.get("compact_evidence_path")
            digest = raw.get("compact_evidence_sha256")
            if not isinstance(value, str) or not isinstance(digest, str):
                continue
            path = Path(value)
            reason: str | None = None
            allocated = 0
            try:
                if (
                    not path.is_absolute()
                    or path == evidence_root
                    or not _is_within(path.resolve(strict=True), evidence_root)
                ):
                    raise RetentionError("COMPACT_EVIDENCE_PATH_UNSAFE")
                current = Path(path.anchor)
                for part in path.parts[1:]:
                    current /= part
                    if stat.S_ISLNK(current.lstat().st_mode):
                        raise RetentionError("COMPACT_EVIDENCE_PATH_UNSAFE")
                metadata = path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise RetentionError("COMPACT_EVIDENCE_PATH_UNSAFE")
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise RetentionError("COMPACT_EVIDENCE_DIGEST_MISMATCH")
                allocated = metadata.st_blocks * 512
                reason = self.process_reference_probe(path)
                reason = reason or self.lock_probe({(metadata.st_dev, metadata.st_ino)})
            except (OSError, RetentionError) as exc:
                reason = exc.code if isinstance(exc, RetentionError) else "COMPACT_EVIDENCE_UNCERTAIN"
            if reason is not None:
                entries.append(
                    CleanupEntryResult(
                        candidate.registration_id,
                        "SKIPPED",
                        reason,
                        allocated,
                    )
                )
                skipped += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            if dry_run:
                entries.append(
                    CleanupEntryResult(
                        candidate.registration_id,
                        "WOULD_CLEAN",
                        "COMPACT_EVIDENCE_EXPIRED",
                        allocated,
                    )
                )
                continue
            try:
                path.unlink()
                raw["compact_evidence_expired_at"] = self.now().isoformat()
                raw["compact_evidence_path"] = None
                raw["compact_evidence_sha256"] = None
                _atomic_write(registration, raw)
            except OSError:
                entries.append(
                    CleanupEntryResult(
                        candidate.registration_id,
                        "FAILED",
                        "COMPACT_EVIDENCE_DELETE_FAILED",
                        allocated,
                    )
                )
                reasons["COMPACT_EVIDENCE_DELETE_FAILED"] = (
                    reasons.get("COMPACT_EVIDENCE_DELETE_FAILED", 0) + 1
                )
                continue
            entries.append(
                CleanupEntryResult(
                    candidate.registration_id,
                    "CLEANED",
                    "COMPACT_EVIDENCE_EXPIRED",
                    allocated,
                )
            )
            cleaned += 1
            reclaimed += allocated
        return entries, cleaned, skipped, reclaimed, reasons

    def ensure_disk_budget(
        self,
        *,
        operation_id: str,
        references: ReferenceSnapshot,
        incoming_allocated_bytes: int = 0,
    ) -> tuple[DiskSnapshot, CleanupRunResult | None]:
        if incoming_allocated_bytes < 0:
            raise RetentionError("INCOMING_SIZE_INVALID")
        before = self.disk_snapshot()
        required_free = self.policy.required_free_bytes(before.total_bytes)
        cleanup: CleanupRunResult | None = None
        if (
            before.disposable_allocated_bytes >= self.policy.cleanup_trigger_bytes
            or before.disposable_allocated_bytes + incoming_allocated_bytes
            > self.policy.hard_limit_bytes
            or before.free_bytes < required_free + incoming_allocated_bytes
        ):
            cleanup = self.sweep(
                operation_id=operation_id,
                references=references,
                dry_run=False,
            )
        after = self.disk_snapshot()
        if (
            after.disposable_allocated_bytes + incoming_allocated_bytes
            > self.policy.hard_limit_bytes
            or after.free_bytes < self.policy.required_free_bytes(after.total_bytes) + incoming_allocated_bytes
        ):
            raise RetentionError("DISK_SPACE_INSUFFICIENT")
        return after, cleanup

    def read_public_status(self) -> dict[str, Any]:
        if not self.status_path.exists():
            return {
                "policy_version": self.policy.policy_version,
                "last_cleanup_at": None,
                "last_cleanup_status": "NOT_RUN",
                "reclaimed_bytes": 0,
                "pending_cleanup_bytes": 0,
                "cleaned_count": 0,
                "skipped_count": 0,
                "skipped_reasons": {},
                "next_retry_at": None,
            }
        value = _read_json(self.status_path)
        if not isinstance(value, dict) or value.get("policy_version") != self.policy.policy_version:
            raise RetentionError("RETENTION_STATUS_INVALID")
        allowed = {
            "policy_version",
            "last_cleanup_at",
            "last_cleanup_status",
            "reclaimed_bytes",
            "pending_cleanup_bytes",
            "cleaned_count",
            "skipped_count",
            "skipped_reasons",
            "next_retry_at",
        }
        if set(value) != allowed:
            raise RetentionError("RETENTION_STATUS_INVALID")
        return value
