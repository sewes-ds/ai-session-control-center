from __future__ import annotations

import hashlib
import json
import re
import socket
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ai_account_manager.core.config import AppSettings, get_settings
from ai_account_manager.core.upgrade_manager import (
    CandidateDescriptor,
    CompatibilityGateResult,
    EngineeringResult,
    TransactionResult,
    UpgradeManagerError,
)
from ai_account_manager.core.upgrade_retention import (
    GIB,
    POLICY_VERSION,
    CleanupCandidate,
    CleanupRunResult,
    ReferenceSnapshot,
    RegisteredRoot,
    RetentionEngine,
    RetentionError,
    RetentionPolicy,
    allocated_size,
    process_reference_reason,
)
from ai_account_manager.providers.codex.upgrade_candidate import (
    MAX_ARCHIVE_BYTES,
    MAX_EXTRACTED_BYTES,
    CandidateProvenance,
)
from ai_account_manager.providers.codex.upgrade_operational import (
    OperationalConfig,
    OperationalError,
    read_private_model,
)


_RELEASE_DIRECTORY = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_PROCESS_INSPECTOR_SOCKET = Path("/run/cus-upgrade-readonly-process-inspector.sock")
_PROCESS_INSPECTOR_MAXIMUM = 4096
_PROCESS_REFERENCE_REASONS = {
    "PROCESS_CWD_REFERENCE",
    "PROCESS_EXE_REFERENCE",
    "PROCESS_MAP_REFERENCE",
    "PROCESS_OPEN_FD_REFERENCE",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_json(path: Path, *, maximum: int = 4 * 1024 * 1024) -> Any:
    if path.is_symlink():
        raise RetentionError("REFERENCE_FILE_UNSAFE")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise RetentionError("REFERENCE_FILE_UNSAFE")
    try:
        return json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise RetentionError("REFERENCE_FILE_INVALID") from exc


def _path_values(value: Any) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, str) and value.startswith("/"):
        paths.append(Path(value))
    elif isinstance(value, dict):
        for item in value.values():
            paths.extend(_path_values(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_path_values(item))
    return paths


def _release_root_for(path: Path, releases: Path) -> Path | None:
    resolved = path.resolve(strict=False)
    release_root = releases.resolve(strict=False)
    try:
        relative = resolved.relative_to(release_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    candidate = release_root / relative.parts[0]
    return candidate if _RELEASE_DIRECTORY.fullmatch(candidate.name) else None


def _registered_root(root_id: str, path: Path) -> RegisteredRoot:
    resolved = path.resolve(strict=True)
    metadata = resolved.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RetentionError("REGISTERED_ROOT_UNSAFE")
    return RegisteredRoot(
        root_id=root_id,
        path=resolved,
        expected_uid=metadata.st_uid,
        expected_gid=metadata.st_gid,
        expected_device=metadata.st_dev,
    )


def _exact_children(root: Path, *, predicate: Any = None) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    values: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            continue
        if predicate is not None and not predicate(child):
            continue
        values.append(child.resolve(strict=True))
    return values


def _safe_inspector_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
    except OSError as exc:
        raise RetentionError("INSPECTION_TOOL_UNAVAILABLE") from exc
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o007
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise RetentionError("INSPECTION_TOOL_UNSAFE")


def _query_process_inspector(
    target: Path,
    *,
    socket_path: Path = _PROCESS_INSPECTOR_SOCKET,
    timeout_seconds: float = 20.0,
) -> str | None:
    """Ask the root-owned read-only inspector about one already validated path."""

    _safe_inspector_socket(socket_path)
    try:
        target_text = str(target.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise RetentionError("INSPECTION_TARGET_UNSAFE") from exc
    target_sha256 = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
    request = json.dumps(
        {"schema_version": 1, "target": target_text},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    response = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)
            while True:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > _PROCESS_INSPECTOR_MAXIMUM:
                    raise RetentionError("INSPECTION_RESULT_AMBIGUOUS")
    except PermissionError as exc:
        raise RetentionError("INSPECTION_PERMISSION_UNAVAILABLE") from exc
    except (OSError, TimeoutError) as exc:
        raise RetentionError("INSPECTION_TOOL_UNAVAILABLE") from exc
    try:
        payload = json.loads(bytes(response))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetentionError("INSPECTION_RESULT_AMBIGUOUS") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"reason", "schema_version", "status", "target_sha256"}
        or payload.get("schema_version") != 1
        or payload.get("target_sha256") != target_sha256
    ):
        raise RetentionError("INSPECTION_RESULT_AMBIGUOUS")
    status = payload.get("status")
    reason = payload.get("reason")
    if status == "CLEAR" and reason is None:
        return None
    if status == "ACTIVE" and reason in _PROCESS_REFERENCE_REASONS:
        return str(reason)
    if status == "AMBIGUOUS" and reason in {
        "INSPECTION_PERMISSION_UNAVAILABLE",
        "INSPECTION_CONFIG_UNSAFE",
        "INSPECTION_RESULT_AMBIGUOUS",
        "INSPECTION_TARGET_UNSAFE",
    }:
        return str(reason)
    raise RetentionError("INSPECTION_RESULT_AMBIGUOUS")


def codex_process_reference_reason(
    target: Path,
    *,
    native_probe: Callable[[Path], str | None] = process_reference_reason,
    inspector_query: Callable[[Path], str | None] = _query_process_inspector,
) -> str | None:
    """Codex adapter that escalates only a permission-blocked read-only scan."""

    reason = native_probe(target)
    if reason != "INSPECTION_PERMISSION_UNAVAILABLE":
        return reason
    try:
        return inspector_query(target)
    except RetentionError as exc:
        return exc.code


class CodexUpgradeRetention:
    """Codex path/reference adapter for the provider-neutral cleanup engine."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        config: OperationalConfig | None = None,
        now: Any = None,
        process_reference_probe: Any = None,
        lock_probe: Any = None,
        delete_tree: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        try:
            self.config = config or read_private_model(
                self.settings.upgrade_runner_config_path,
                OperationalConfig,
                code="RETENTION_CONFIG_INVALID",
            )
            self.now = now or _utc_now
            base = Path(self.config.dedicated_test_profile).resolve(strict=False).parent.parent
            self.retention_root = base / "retention"
            self.qualification_root = Path(self.config.dedicated_test_profile).resolve(
                strict=False
            ).parent
            self.production_root = Path(self.config.root).resolve(strict=True)
            self.releases_root = self.production_root / "candidate-store" / "releases"
            self.engineering_root = Path(self.config.engineering_root).resolve(strict=True)
            self.shared_codex_home = Path(self.config.dedicated_test_profile).resolve(strict=False)
            roots = (
                _registered_root("production-releases", self.releases_root),
                _registered_root("qualification-attempts", self.qualification_root),
                _registered_root("engineering-attempts", self.engineering_root),
            )
        except (OSError, OperationalError, RetentionError) as exc:
            code = exc.code if isinstance(exc, (OperationalError, RetentionError)) else "RETENTION_CONFIG_INVALID"
            raise UpgradeManagerError(code) from exc
        kwargs: dict[str, Any] = {}
        kwargs["process_reference_probe"] = (
            process_reference_probe or codex_process_reference_reason
        )
        if lock_probe is not None:
            kwargs["lock_probe"] = lock_probe
        if delete_tree is not None:
            kwargs["delete_tree"] = delete_tree
        self.engine = RetentionEngine(
            state_root=self.retention_root,
            roots=roots,
            usage_paths=self._usage_paths(),
            filesystem_path=self.production_root,
            policy=RetentionPolicy(),
            now=self.now,
            **kwargs,
        )

    def _usage_paths(self) -> tuple[Path, ...]:
        releases = _exact_children(
            self.releases_root,
            predicate=lambda path: bool(_RELEASE_DIRECTORY.fullmatch(path.name)),
        )
        qualification = _exact_children(
            self.qualification_root,
            predicate=lambda path: path.name not in {"codex-home", "retention"},
        )
        engineering = _exact_children(self.engineering_root)
        return tuple(releases + qualification + engineering)

    def _active_and_previous(self) -> tuple[Path | None, Path | None]:
        active: Path | None = None
        previous: Path | None = None
        active_reference = Path(self.config.active_reference)
        if active_reference.is_file():
            value = _read_json(active_reference)
            if isinstance(value, dict) and isinstance(value.get("binary_path"), str):
                active = _release_root_for(Path(value["binary_path"]), self.releases_root)
        transactions = Path(self.config.deployment_root) / "transactions"
        snapshots: list[tuple[int, Path]] = []
        if transactions.is_dir() and not transactions.is_symlink():
            for transaction in transactions.iterdir():
                snapshot = transaction / "snapshot" / "active_reference"
                if transaction.is_symlink() or not snapshot.is_file() or snapshot.is_symlink():
                    continue
                try:
                    value = _read_json(snapshot)
                    raw = value.get("binary_path") if isinstance(value, dict) else None
                    candidate = _release_root_for(Path(raw), self.releases_root) if isinstance(raw, str) else None
                    if candidate is not None and candidate != active:
                        snapshots.append((snapshot.lstat().st_mtime_ns, candidate))
                except (OSError, RetentionError):
                    continue
        if snapshots:
            previous = max(snapshots, key=lambda item: item[0])[1]
        return active, previous

    def references(
        self,
        *,
        active_job_ids: Sequence[str] = (),
        active_attempt_ids: Sequence[str] = (),
        extra_keep_paths: Sequence[Path] = (),
        completed_candidate_digests: Sequence[str] = (),
    ) -> ReferenceSnapshot:
        active, previous = self._active_and_previous()
        keep = [self.shared_codex_home, *extra_keep_paths]
        if active is not None:
            keep.append(active)
        if previous is not None:
            keep.append(previous)
        paths: list[Path] = []
        digests: list[str] = []
        for reference_file in (
            Path(self.config.active_reference),
            Path(self.config.runtime_specs_path),
            Path(self.config.project_state_path),
            Path(self.config.service_state_path),
            Path(self.config.sessions_state_path),
            Path(self.config.compatibility_state_path),
            self.settings.upgrade_runtime_root / "candidate.json",
            self.settings.upgrade_runtime_root / "candidate-provenance.json",
        ):
            if not reference_file.is_file() or reference_file.is_symlink():
                continue
            try:
                value = _read_json(reference_file)
            except RetentionError:
                continue
            paths.extend(_path_values(value))
            if isinstance(value, dict):
                for key in ("digest_sha256", "binary_sha256", "sha256"):
                    raw = value.get(key)
                    if isinstance(raw, str) and re.fullmatch(r"[0-9a-f]{64}", raw):
                        digests.append(raw)
        tmux_paths: list[Path] = []
        session_records = self.production_root / "engineering-sessions"
        if session_records.is_dir() and not session_records.is_symlink():
            for record in sorted(session_records.iterdir(), key=lambda item: item.name):
                if not record.is_file() or record.is_symlink():
                    continue
                try:
                    value = _read_json(record)
                except RetentionError:
                    continue
                raw = value.get("session_root") if isinstance(value, dict) else None
                if isinstance(raw, str) and raw.startswith("/"):
                    tmux_paths.append(Path(raw))
        try:
            tmux = subprocess.run(
                [
                    "/usr/bin/tmux",
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_current_path}",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            tmux = None
        if tmux is not None and tmux.returncode == 0 and len(tmux.stdout) <= 1024 * 1024:
            for line in tmux.stdout.decode("utf-8", errors="replace").splitlines():
                if line.startswith("/"):
                    tmux_paths.append(Path(line))
        return ReferenceSnapshot(
            keep_paths=tuple(dict.fromkeys(path.resolve(strict=False) for path in keep)),
            active_reference_paths=tuple(
                dict.fromkeys(path.resolve(strict=False) for path in paths)
            ),
            active_tmux_paths=tuple(
                dict.fromkeys(path.resolve(strict=False) for path in tmux_paths)
            ),
            active_job_ids=tuple(active_job_ids),
            active_attempt_ids=tuple(active_attempt_ids),
            active_candidate_digests=tuple(dict.fromkeys(digests)),
            completed_candidate_digests=tuple(completed_candidate_digests),
        )

    def historical_paths(self) -> tuple[Path, ...]:
        registered = {item.path.resolve(strict=False) for item in self.engine.registrations()}
        return tuple(path for path in self._usage_paths() if path not in registered)

    def _candidate_evidence(
        self,
        candidate: CandidateDescriptor,
        *,
        gate: CompatibilityGateResult | None,
        failure_codes: Sequence[str],
        commands: Sequence[dict[str, Any]],
        test_summary: dict[str, Any],
        artifact_hashes: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "candidate_version": candidate.binding.version,
            "candidate_sha256": candidate.binding.digest_sha256,
            "compatibility_binding": candidate.binding.model_dump(mode="json"),
            "gate_statuses": (
                [item.model_dump(mode="json") for item in gate.checks] if gate else []
            ),
            "commands": list(commands),
            "timestamps": {"completed_at": self.now().isoformat()},
            "failure_or_block_reason": list(failure_codes),
            "test_summary": test_summary,
            "artifact_hashes": artifact_hashes,
        }

    def register_candidate_release(
        self,
        candidate: CandidateDescriptor,
        provenance: CandidateProvenance,
    ) -> None:
        release = _release_root_for(Path(candidate.binary_path), self.releases_root)
        if release is None:
            raise UpgradeManagerError("RETENTION_CANDIDATE_ROOT_INVALID")
        metadata = release.lstat()
        registration_id = "release-" + hashlib.sha256(str(release).encode()).hexdigest()[:24]
        evidence = self._candidate_evidence(
            candidate,
            gate=None,
            failure_codes=(),
            commands=({"argv": ["official-release-stage"], "exit_code": 0},),
            test_summary={"stage_candidate": "PASSED"},
            artifact_hashes={
                "binary": candidate.binding.digest_sha256,
                "archive": provenance.archive_sha256 or candidate.binding.digest_sha256,
            },
        )
        for existing in self.engine.registrations():
            if existing.registration_id != registration_id:
                continue
            existing_digest = str(existing.compact_evidence.get("candidate_sha256") or "")
            if existing.path == release and existing_digest == candidate.binding.digest_sha256:
                return
            raise UpgradeManagerError("RETENTION_REGISTRATION_CONFLICT")
        self.engine.register(
            CleanupCandidate(
                registration_id=registration_id,
                root_id="production-releases",
                path=release,
                task_id="upgrade-manager",
                job_id="release-discovery",
                attempt_id=f"{candidate.binding.version}-{candidate.binding.digest_sha256[:16]}",
                kind="tree",
                completed_at=self.now().isoformat(),
                failed=False,
                expected_uid=metadata.st_uid,
                expected_gid=metadata.st_gid,
                expected_device=metadata.st_dev,
                implementation_commit=candidate.binding.cus_commit,
                evidence_commit=candidate.binding.cus_commit,
                compact_evidence=evidence,
            )
        )

    def register_qualification_attempt(
        self,
        *,
        attempt_root: Path,
        task_id: str,
        job_id: str,
        attempt_id: str,
        candidate: CandidateDescriptor,
        gate: CompatibilityGateResult | None,
        failed: bool,
        commands: Sequence[dict[str, Any]],
        test_summary: dict[str, Any],
        artifact_hashes: dict[str, str],
        implementation_commit: str,
        evidence_commit: str | None = None,
    ) -> None:
        target = attempt_root.resolve(strict=True)
        metadata = target.lstat()
        registration_id = "qualification-" + hashlib.sha256(str(target).encode()).hexdigest()[:24]
        self.engine.register(
            CleanupCandidate(
                registration_id=registration_id,
                root_id="qualification-attempts",
                path=target,
                task_id=task_id,
                job_id=job_id,
                attempt_id=attempt_id,
                kind="tree",
                completed_at=self.now().isoformat(),
                failed=failed,
                expected_uid=metadata.st_uid,
                expected_gid=metadata.st_gid,
                expected_device=metadata.st_dev,
                implementation_commit=implementation_commit,
                evidence_commit=evidence_commit,
                compact_evidence=self._candidate_evidence(
                    candidate,
                    gate=gate,
                    failure_codes=gate.failure_codes if gate else (),
                    commands=commands,
                    test_summary=test_summary,
                    artifact_hashes=artifact_hashes,
                ),
            )
        )

    def before_candidate_download(self) -> None:
        try:
            self.engine.ensure_disk_budget(
                operation_id=f"pre-download-{int(self.now().timestamp())}",
                references=self.references(),
                incoming_allocated_bytes=MAX_ARCHIVE_BYTES + MAX_EXTRACTED_BYTES,
            )
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc

    def before_attempt(self, candidate: CandidateDescriptor, attempt: int) -> None:
        try:
            self.engine.ensure_disk_budget(
                operation_id=f"pre-attempt-{attempt}-{int(self.now().timestamp())}",
                references=self.references(
                    active_attempt_ids=(str(attempt),),
                    extra_keep_paths=(Path(candidate.binary_path),),
                ),
                incoming_allocated_bytes=GIB,
            )
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc

    def complete_attempt(
        self,
        candidate: CandidateDescriptor,
        attempt: int,
        session: EngineeringResult,
        gate: CompatibilityGateResult | None,
        failure_codes: list[str],
    ) -> None:
        session_root = Path(session.worktree_path).resolve(strict=True).parent
        metadata = session_root.lstat()
        registration_id = "engineering-" + hashlib.sha256(
            str(session_root).encode()
        ).hexdigest()[:24]
        try:
            size = allocated_size(session_root)[0]
            self.engine.register(
                CleanupCandidate(
                    registration_id=registration_id,
                    root_id="engineering-attempts",
                    path=session_root,
                    task_id="upgrade-manager",
                    job_id="engineering-repair",
                    attempt_id=str(attempt),
                    kind="tree",
                    completed_at=self.now().isoformat(),
                    failed=bool(failure_codes),
                    expected_uid=metadata.st_uid,
                    expected_gid=metadata.st_gid,
                    expected_device=metadata.st_dev,
                    implementation_commit=session.cus_commit,
                    evidence_commit=session.cus_commit,
                    compact_evidence=self._candidate_evidence(
                        candidate,
                        gate=gate,
                        failure_codes=failure_codes,
                        commands=({"argv": ["engineering-session"], "exit_code": 0},),
                        test_summary={
                            "fresh_session": session.fresh_session,
                            "production_unchanged": session.production_unchanged,
                            "allocated_bytes": size,
                        },
                        artifact_hashes={"engineering_evidence": session.evidence_sha256},
                    ),
                )
            )
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc

    def after_active(
        self,
        candidate: CandidateDescriptor,
        transaction: TransactionResult,
    ) -> None:
        try:
            result = self.engine.sweep(
                operation_id=f"post-active-{candidate.binding.version}-{int(self.now().timestamp())}",
                references=self.references(
                    extra_keep_paths=(Path(candidate.binary_path),),
                    completed_candidate_digests=(candidate.binding.digest_sha256,),
                ),
                dry_run=False,
            )
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc
        if result.status == "FAILED":
            raise UpgradeManagerError("RETENTION_CLEANUP_FAILED")

    def sweep(
        self,
        *,
        operation_id: str,
        dry_run: bool,
        include_historical: bool = False,
    ) -> CleanupRunResult:
        try:
            return self.engine.sweep(
                operation_id=operation_id,
                references=self.references(),
                dry_run=dry_run,
                historical_paths=self.historical_paths() if include_historical else (),
            )
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc

    def public_status(self) -> dict[str, Any]:
        try:
            status = self.engine.read_public_status()
            disk = self.engine.disk_snapshot()
        except RetentionError as exc:
            raise UpgradeManagerError(exc.code) from exc
        status.update(
            {
                "cleanup_trigger_bytes": self.engine.policy.cleanup_trigger_bytes,
                "hard_limit_bytes": self.engine.policy.hard_limit_bytes,
                "minimum_free_bytes": self.engine.policy.required_free_bytes(
                    disk.total_bytes
                ),
                "disposable_usage_bytes": disk.disposable_allocated_bytes,
                "filesystem_free_bytes": disk.free_bytes,
            }
        )
        return status


def policy_document() -> dict[str, Any]:
    policy = RetentionPolicy()
    return {
        "policy_version": POLICY_VERSION,
        "cleanup_trigger_bytes": policy.cleanup_trigger_bytes,
        "hard_limit_bytes": policy.hard_limit_bytes,
        "minimum_free_bytes": policy.minimum_free_bytes,
        "minimum_free_percent": policy.minimum_free_percent,
        "latest_failed_ttl_seconds": policy.latest_failed_ttl_seconds,
        "compact_evidence_retention_seconds": policy.compact_evidence_retention_seconds,
        "heavy_production_releases": ["ACTIVE", "PREVIOUS_KNOWN_GOOD"],
    }


class LazyCodexUpgradeRetention:
    """Delay private production config reads until an operation actually runs."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def _adapter(self) -> CodexUpgradeRetention:
        return CodexUpgradeRetention(settings=self.settings)

    def before_candidate_download(self) -> None:
        self._adapter().before_candidate_download()

    def register_candidate_release(
        self,
        candidate: CandidateDescriptor,
        provenance: CandidateProvenance,
    ) -> None:
        self._adapter().register_candidate_release(candidate, provenance)

    def before_attempt(self, candidate: CandidateDescriptor, attempt: int) -> None:
        self._adapter().before_attempt(candidate, attempt)

    def complete_attempt(
        self,
        candidate: CandidateDescriptor,
        attempt: int,
        session: EngineeringResult,
        gate: CompatibilityGateResult | None,
        failure_codes: list[str],
    ) -> None:
        self._adapter().complete_attempt(
            candidate, attempt, session, gate, failure_codes
        )

    def after_active(
        self,
        candidate: CandidateDescriptor,
        transaction: TransactionResult,
    ) -> None:
        self._adapter().after_active(candidate, transaction)
