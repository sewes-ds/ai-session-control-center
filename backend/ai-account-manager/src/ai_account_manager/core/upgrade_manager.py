from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
import fcntl
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError


SCHEMA_VERSION = 1
MAX_REPAIR_CYCLES = 5
MAX_JSON_BYTES = 256 * 1024
F = TypeVar("F", bound=Callable[..., object])

REQUIRED_GATE_CHECKS = (
    "candidate_version",
    "launcher_runtime_spec",
    "telegram_to_candidate_input",
    "candidate_to_telegram_final",
    "reasoning_not_final",
    "tool_not_final",
    "system_not_final",
    "duplicate_final_suppressed",
    "completion_without_final_fails",
    "session_resume_history",
    "f4_runtime_truth",
    "combined_overlay_build",
    "telegram_bot_import_origin",
    "canonical_compatibility_contract",
    "rollout_schema_known",
)

REQUIRED_ACTIVATION_STEPS = (
    "rollback_bundle_created",
    "candidate_activated",
    "runtime_specs_reconciled",
    "stopped_projects_not_started",
    "combined_overlay_activated",
    "binary_origin_verified",
    "telegram_bot_import_origin_verified",
    "services_healthy",
    "post_upgrade_smoke",
    "running_sessions_preserved",
)

REQUIRED_ROLLBACK_STEPS = (
    "codex_restored",
    "runtime_specs_restored",
    "overlay_restored",
    "compatibility_state_restored",
    "origins_verified",
    "services_healthy",
)


class UpgradeState(str, Enum):
    CURRENT = "CURRENT"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    CHECKING = "CHECKING"
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    CHECK_FAILED = "CHECK_FAILED"
    EVIDENCE_READY = "EVIDENCE_READY"
    REPAIRING = "REPAIRING"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    UPDATING = "UPDATING"
    ACTIVE = "ACTIVE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLBACK = "ROLLBACK"
    BLOCKED = "BLOCKED"


class GateVerdict(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    CHECK_FAILED = "CHECK_FAILED"


class StepStatus(str, Enum):
    PASS = "PASS"
    INCOMPATIBLE = "INCOMPATIBLE"
    ERROR = "ERROR"


class CandidateBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cus_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    telegram_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    combined_overlay_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility_gate_version: str = Field(min_length=1, max_length=80)


class CandidateDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    active_version: str | None = Field(
        default=None,
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
    )
    binary_path: str = Field(min_length=1, max_length=4096)
    binding: CandidateBinding


class ReleaseDiscoveryResult(BaseModel):
    """Provider adapter result consumed by the provider-neutral manager."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    active_version: str = Field(min_length=1, max_length=160)
    active_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovered_at: str = Field(min_length=1, max_length=80)
    candidate: CandidateDescriptor | None = None
    failure_code: str | None = Field(default=None, max_length=160)


class CheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: StepStatus
    failure_code: str | None = Field(default=None, max_length=160)


class CompatibilityGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: GateVerdict
    binding: CandidateBinding
    checks: list[CheckEvidence]
    failure_codes: list[str] = Field(default_factory=list, max_length=64)


class CompatibilityPass(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding: CandidateBinding
    checks: list[CheckEvidence]


class EngineeringResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=160)
    tmux_session_name: str = Field(min_length=1, max_length=160)
    fresh_session: bool
    codex_process_started: bool
    worktree_clean_at_start: bool
    changed_scope_validated: bool
    production_unchanged: bool
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    worktree_path: str = Field(min_length=1, max_length=4096)
    cus_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    telegram_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    combined_overlay_identity: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding: CandidateBinding
    steps: dict[str, StepStatus]
    failure_code: str | None = Field(default=None, max_length=160)


class UpgradePipelineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    state: UpgradeState
    active_version: str | None = None
    active_digest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate: CandidateDescriptor | None = None
    compatibility_pass: CompatibilityPass | None = None
    last_gate: CompatibilityGateResult | None = None
    repair_attempt: int = Field(default=0, ge=0, le=MAX_REPAIR_CYCLES)
    max_repair_cycles: Literal[5] = MAX_REPAIR_CYCLES
    evidence_path: str | None = None
    last_failure_codes: list[str] = Field(default_factory=list, max_length=64)
    discovery_refreshed_at: str | None = None
    discovery_error_code: str | None = Field(default=None, max_length=160)


class UpgradeStatus(BaseModel):
    provider_id: str
    state: UpgradeState
    current_version: str | None
    candidate_version: str | None
    action: str
    busy: bool
    repair_attempt: int
    max_repair_cycles: Literal[5] = MAX_REPAIR_CYCLES
    machine_result: GateVerdict | None = None
    discovery_refreshed_at: str | None = None
    discovery_error_code: str | None = None


class UpgradeMaintenanceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str
    last_cleanup_at: str | None
    last_cleanup_status: str
    reclaimed_bytes: int = Field(ge=0)
    pending_cleanup_bytes: int = Field(ge=0)
    cleaned_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    skipped_reasons: dict[str, int]
    next_retry_at: str | None
    cleanup_trigger_bytes: int = Field(gt=0)
    hard_limit_bytes: int = Field(gt=0)
    minimum_free_bytes: int = Field(gt=0)
    disposable_usage_bytes: int = Field(ge=0)
    filesystem_free_bytes: int = Field(ge=0)


class UpgradeManagerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CompatibilityGate(Protocol):
    def run(self, candidate: CandidateDescriptor) -> CompatibilityGateResult: ...


class EngineeringSessionRunner(Protocol):
    def start(
        self, candidate: CandidateDescriptor, evidence_path: Path, attempt: int
    ) -> EngineeringResult: ...

    def close(self, session_id: str) -> None: ...


class ActivationRunner(Protocol):
    def apply(self, candidate: CandidateDescriptor) -> TransactionResult: ...

    def rollback(self, candidate: CandidateDescriptor) -> TransactionResult: ...


class ReleaseDiscovery(Protocol):
    def discover(self) -> ReleaseDiscoveryResult: ...


class UpgradeRetention(Protocol):
    """Provider adapter for cleanup lifecycle hooks.

    Cleanup bookkeeping is deliberately outside the public pipeline record.  A
    cleanup failure may schedule a retry, but it must never turn a verified
    activation into a rollback.
    """

    def before_attempt(self, candidate: CandidateDescriptor, attempt: int) -> None: ...

    def complete_attempt(
        self,
        candidate: CandidateDescriptor,
        attempt: int,
        session: EngineeringResult,
        gate: CompatibilityGateResult | None,
        failure_codes: list[str],
    ) -> None: ...

    def after_active(
        self,
        candidate: CandidateDescriptor,
        transaction: TransactionResult,
    ) -> None: ...


def _assert_safe_root(root: Path, *, create: bool) -> None:
    if not root.is_absolute() or ".." in root.parts or root == Path("/"):
        raise UpgradeManagerError("unsafe_runtime_root")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise UpgradeManagerError("unsafe_runtime_root")
    if not root.exists():
        if not create:
            return
        root.mkdir(mode=0o700, parents=True)
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise UpgradeManagerError("unsafe_runtime_root")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UpgradeManagerError("unsafe_runtime_permissions")


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel | None:
    if path.is_symlink():
        raise UpgradeManagerError("unsafe_state_file")
    if not path.exists():
        return None
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise UpgradeManagerError("unsafe_state_file")
    if stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > MAX_JSON_BYTES:
        raise UpgradeManagerError("unsafe_state_file")
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise UpgradeManagerError("invalid_state_file") from exc


def _atomic_write(path: Path, payload: BaseModel | dict[str, object]) -> None:
    serialized = (
        json.dumps(
            payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(serialized) > MAX_JSON_BYTES:
        raise UpgradeManagerError("state_too_large")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise UpgradeManagerError("unsafe_state_file")
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class UpgradeStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def candidate_path(self) -> Path:
        return self.root / "candidate.json"

    @property
    def state_path(self) -> Path:
        return self.root / "pipeline-state.json"

    def read_candidate(self) -> CandidateDescriptor | None:
        _assert_safe_root(self.root, create=False)
        value = _read_model(self.candidate_path, CandidateDescriptor)
        return value if isinstance(value, CandidateDescriptor) else None

    def read_state(self) -> UpgradePipelineRecord | None:
        _assert_safe_root(self.root, create=False)
        value = _read_model(self.state_path, UpgradePipelineRecord)
        return value if isinstance(value, UpgradePipelineRecord) else None

    def write_state(self, record: UpgradePipelineRecord) -> None:
        _assert_safe_root(self.root, create=True)
        _atomic_write(self.state_path, record)

    def write_candidate(self, candidate: CandidateDescriptor) -> None:
        _assert_safe_root(self.root, create=True)
        _atomic_write(self.candidate_path, candidate)

    def clear_candidate(self) -> None:
        _assert_safe_root(self.root, create=False)
        if not self.candidate_path.exists():
            return
        _read_model(self.candidate_path, CandidateDescriptor)
        self.candidate_path.unlink()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def write_operator_action(self, operation_id: str, payload: dict[str, object]) -> Path:
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", operation_id):
            raise UpgradeManagerError("operation_id_invalid")
        _assert_safe_root(self.root, create=True)
        actions = self.root / "operator-actions"
        _assert_safe_root(actions, create=True)
        path = actions / f"{operation_id}.json"
        if path.exists():
            raise UpgradeManagerError("operation_id_reused")
        _atomic_write(path, payload)
        return path

    def write_evidence(self, attempt: int, payload: dict[str, object]) -> Path:
        _assert_safe_root(self.root, create=True)
        path = self.root / f"repair-evidence-{attempt}.json"
        _atomic_write(path, payload)
        return path

    def evidence_digest(self, attempt: int, supplied_path: Path) -> str:
        expected = self.root / f"repair-evidence-{attempt}.json"
        if supplied_path != expected or supplied_path.is_symlink():
            raise UpgradeManagerError("repair_evidence_unsafe")
        try:
            metadata = supplied_path.lstat()
            payload = supplied_path.read_bytes()
        except OSError as exc:
            raise UpgradeManagerError("repair_evidence_unsafe") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_JSON_BYTES
        ):
            raise UpgradeManagerError("repair_evidence_unsafe")
        return hashlib.sha256(payload).hexdigest()

    @contextmanager
    def operation_lock(self) -> Iterator[None]:
        _assert_safe_root(self.root, create=True)
        lock_path = self.root / ".operation.lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise UpgradeManagerError("operation_lock_unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise UpgradeManagerError("operation_lock_unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpgradeManagerError("operation_in_progress") from exc
            except OSError as exc:
                raise UpgradeManagerError("operation_lock_failed") from exc
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _serialized_operation(method: F) -> F:
    @wraps(method)
    def wrapped(self: SafeUpgradeManager, *args: Any, **kwargs: Any) -> object:
        with self.store.operation_lock():
            return method(self, *args, **kwargs)

    return cast(F, wrapped)


def _check_map(checks: list[CheckEvidence]) -> dict[str, CheckEvidence] | None:
    mapping: dict[str, CheckEvidence] = {}
    for check in checks:
        if check.check_id in mapping:
            return None
        mapping[check.check_id] = check
    return mapping


def normalize_gate_result(
    candidate: CandidateDescriptor, raw: CompatibilityGateResult
) -> CompatibilityGateResult:
    if raw.binding != candidate.binding:
        return CompatibilityGateResult(
            verdict=GateVerdict.CHECK_FAILED,
            binding=candidate.binding,
            checks=[],
            failure_codes=["gate_binding_mismatch"],
        )
    mapping = _check_map(raw.checks)
    if mapping is None or set(mapping) != set(REQUIRED_GATE_CHECKS):
        return CompatibilityGateResult(
            verdict=GateVerdict.CHECK_FAILED,
            binding=candidate.binding,
            checks=raw.checks,
            failure_codes=["gate_check_set_invalid"],
        )
    failures = [
        check.failure_code or f"{check.check_id}:{check.status.value.lower()}"
        for check in raw.checks
        if check.status is not StepStatus.PASS
    ]
    if any(check.status is StepStatus.ERROR for check in raw.checks):
        verdict = GateVerdict.CHECK_FAILED
    elif any(check.status is StepStatus.INCOMPATIBLE for check in raw.checks):
        verdict = GateVerdict.INCOMPATIBLE
    else:
        verdict = GateVerdict.COMPATIBLE
    return CompatibilityGateResult(
        verdict=verdict,
        binding=candidate.binding,
        checks=raw.checks,
        failure_codes=failures,
    )


class SafeUpgradeManager:
    def __init__(
        self,
        store: UpgradeStateStore,
        gate: CompatibilityGate,
        engineering: EngineeringSessionRunner,
        activation: ActivationRunner,
        *,
        provider_id: str,
        compatibility_gate_version: str,
        release_discovery: ReleaseDiscovery | None = None,
        retention: UpgradeRetention | None = None,
        discovery_ttl_seconds: int = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.gate = gate
        self.engineering = engineering
        self.activation = activation
        self.provider_id = provider_id
        self.compatibility_gate_version = compatibility_gate_version
        self.release_discovery = release_discovery
        self.retention = retention
        self.discovery_ttl_seconds = discovery_ttl_seconds
        self.now = now or (lambda: datetime.now(UTC))

    def _discovery_is_fresh(self, record: UpgradePipelineRecord) -> bool:
        if record.discovery_refreshed_at is None:
            return False
        try:
            refreshed = datetime.fromisoformat(record.discovery_refreshed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if refreshed.tzinfo is None:
            return False
        return self.now() < refreshed + timedelta(seconds=self.discovery_ttl_seconds)

    def _write_operator_action(
        self,
        *,
        operation_id: str | None,
        action: Literal["CHECK", "UPDATE"],
        candidate: CandidateDescriptor,
        record: UpgradePipelineRecord,
        runner_identity: dict[str, str] | None,
        started_at: str,
        transaction: TransactionResult | None = None,
        rollback: TransactionResult | None = None,
    ) -> None:
        if operation_id is None:
            return
        binding_payload = candidate.binding.model_dump(mode="json")
        binding_sha256 = hashlib.sha256(
            json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.store.write_operator_action(
            operation_id,
            {
                "schema_version": SCHEMA_VERSION,
                "operation_id": operation_id,
                "action": action,
                "started_at": started_at,
                "completed_at": self.now().isoformat(),
                "serialized_operation_lock": True,
                "candidate_binding": binding_payload,
                "candidate_binding_sha256": binding_sha256,
                "runner_identity": runner_identity or {},
                "resulting_state": record.state.value,
                "machine_result": record.last_gate.verdict.value if record.last_gate else None,
                "checks": [
                    check.model_dump(mode="json")
                    for check in (record.last_gate.checks if record.last_gate else [])
                ],
                "production_codex_mutated": action == "UPDATE" and record.state is UpgradeState.ACTIVE,
                "transaction": transaction.model_dump(mode="json") if transaction else None,
                "rollback": rollback.model_dump(mode="json") if rollback else None,
            },
        )

    def _record(self) -> UpgradePipelineRecord:
        candidate = self.store.read_candidate()
        if candidate is not None and candidate.binding.provider_id != self.provider_id:
            raise UpgradeManagerError("candidate_provider_mismatch")
        existing = self.store.read_state()
        if existing is not None:
            if candidate is not None and existing.candidate is None:
                return UpgradePipelineRecord(
                    state=UpgradeState.UPDATE_AVAILABLE,
                    active_version=candidate.active_version,
                    candidate=candidate,
                )
            if candidate is not None and existing.candidate != candidate:
                return UpgradePipelineRecord(
                    state=UpgradeState.UPDATE_AVAILABLE,
                    active_version=candidate.active_version,
                    candidate=candidate,
                )
            return existing
        if candidate is None or candidate.binding.version == candidate.active_version:
            return UpgradePipelineRecord(
                state=UpgradeState.CURRENT,
                active_version=candidate.active_version if candidate else None,
                candidate=candidate,
            )
        return UpgradePipelineRecord(
            state=UpgradeState.UPDATE_AVAILABLE,
            active_version=candidate.active_version,
            candidate=candidate,
        )

    def status(self) -> UpgradeStatus:
        record = self._record()
        action_by_state = {
            UpgradeState.UPDATE_AVAILABLE: "CHECK",
            UpgradeState.CHECK_FAILED: "CHECK",
            UpgradeState.COMPATIBLE: "UPDATE",
            UpgradeState.INCOMPATIBLE: "PREPARE",
            UpgradeState.EVIDENCE_READY: "REPAIR",
        }
        busy = record.state in {
            UpgradeState.CHECKING,
            UpgradeState.REPAIRING,
            UpgradeState.UPDATING,
            UpgradeState.ROLLING_BACK,
        }
        return UpgradeStatus(
            provider_id=self.provider_id,
            state=record.state,
            current_version=record.active_version,
            candidate_version=record.candidate.binding.version if record.candidate else None,
            action=action_by_state.get(record.state, "NONE"),
            busy=busy,
            repair_attempt=record.repair_attempt,
            machine_result=record.last_gate.verdict if record.last_gate else None,
            discovery_refreshed_at=record.discovery_refreshed_at,
            discovery_error_code=record.discovery_error_code,
        )

    @_serialized_operation
    def refresh_release(self) -> UpgradeStatus:
        record = self._record()
        if self._discovery_is_fresh(record):
            return self.status()
        if self.release_discovery is None:
            raise UpgradeManagerError("release_discovery_not_configured")
        try:
            result = self.release_discovery.discover()
        except UpgradeManagerError as exc:
            record.discovery_refreshed_at = self.now().isoformat()
            record.discovery_error_code = exc.code
            record.last_failure_codes = [exc.code]
            self.store.write_state(record)
            return self.status()
        if result.provider_id != self.provider_id:
            raise UpgradeManagerError("release_discovery_provider_mismatch")
        record.active_version = result.active_version
        record.active_digest_sha256 = result.active_digest_sha256
        record.discovery_refreshed_at = result.discovered_at
        record.discovery_error_code = result.failure_code
        if result.failure_code is not None:
            record.last_failure_codes = [result.failure_code]
            self.store.write_state(record)
            return self.status()

        candidate = result.candidate
        if candidate is None:
            self.store.clear_candidate()
            record = UpgradePipelineRecord(
                state=UpgradeState.CURRENT,
                active_version=result.active_version,
                active_digest_sha256=result.active_digest_sha256,
                discovery_refreshed_at=result.discovered_at,
            )
            self.store.write_state(record)
            return self.status()
        if (
            candidate.binding.provider_id != self.provider_id
            or candidate.active_version != result.active_version
            or candidate.binding.compatibility_gate_version != self.compatibility_gate_version
            or tuple(map(int, candidate.binding.version.split(".")))
            <= tuple(map(int, result.active_version.split(".")))
        ):
            raise UpgradeManagerError("release_discovery_candidate_invalid")
        existing = record.candidate
        self.store.write_candidate(candidate)
        if existing == candidate and record.compatibility_pass is not None:
            record.candidate = candidate
            record.discovery_error_code = None
            record.last_failure_codes = []
        else:
            record = UpgradePipelineRecord(
                state=UpgradeState.UPDATE_AVAILABLE,
                active_version=result.active_version,
                active_digest_sha256=result.active_digest_sha256,
                candidate=candidate,
                discovery_refreshed_at=result.discovered_at,
            )
        self.store.write_state(record)
        return self.status()

    @_serialized_operation
    def check(
        self,
        *,
        operation_id: str | None = None,
        runner_identity: dict[str, str] | None = None,
    ) -> UpgradeStatus:
        record = self._record()
        if record.candidate is None or record.state not in {
            UpgradeState.UPDATE_AVAILABLE,
            UpgradeState.CHECK_FAILED,
        }:
            raise UpgradeManagerError("candidate_not_checkable")
        candidate_at_click = record.candidate
        started_at = self.now().isoformat()
        record.state = UpgradeState.CHECKING
        record.compatibility_pass = None
        self.store.write_state(record)
        try:
            raw_result = self.gate.run(record.candidate)
        except UpgradeManagerError as exc:
            record.state = UpgradeState.CHECK_FAILED
            record.last_failure_codes = [exc.code]
            record.last_gate = CompatibilityGateResult(
                verdict=GateVerdict.CHECK_FAILED,
                binding=record.candidate.binding,
                checks=[],
                failure_codes=[exc.code],
            )
            self.store.write_state(record)
            self._write_operator_action(
                operation_id=operation_id,
                action="CHECK",
                candidate=candidate_at_click,
                record=record,
                runner_identity=runner_identity,
                started_at=started_at,
            )
            return self.status()
        normalized = normalize_gate_result(record.candidate, raw_result)
        record.last_gate = normalized
        record.last_failure_codes = normalized.failure_codes
        if normalized.verdict is GateVerdict.COMPATIBLE:
            record.state = UpgradeState.COMPATIBLE
            record.compatibility_pass = CompatibilityPass(
                binding=record.candidate.binding,
                checks=normalized.checks,
            )
        elif normalized.verdict is GateVerdict.INCOMPATIBLE:
            record.state = UpgradeState.INCOMPATIBLE
        else:
            record.state = UpgradeState.CHECK_FAILED
        self.store.write_state(record)
        self._write_operator_action(
            operation_id=operation_id,
            action="CHECK",
            candidate=candidate_at_click,
            record=record,
            runner_identity=runner_identity,
            started_at=started_at,
        )
        return self.status()

    @_serialized_operation
    def prepare(self) -> UpgradeStatus:
        record = self._record()
        if record.state is not UpgradeState.INCOMPATIBLE or record.candidate is None:
            raise UpgradeManagerError("incompatible_evidence_required")
        path = self.store.write_evidence(
            record.repair_attempt + 1,
            {
                "schema_version": SCHEMA_VERSION,
                "binding": record.candidate.binding.model_dump(mode="json"),
                "failure_codes": record.last_failure_codes,
                "checks": [
                    check.model_dump(mode="json")
                    for check in (record.last_gate.checks if record.last_gate else [])
                ],
            },
        )
        record.evidence_path = str(path)
        record.state = UpgradeState.EVIDENCE_READY
        self.store.write_state(record)
        return self.status()

    @_serialized_operation
    def repair(self) -> UpgradeStatus:
        record = self._record()
        if (
            record.state is not UpgradeState.EVIDENCE_READY
            or record.candidate is None
            or not record.evidence_path
        ):
            raise UpgradeManagerError("repair_evidence_not_ready")
        seen_sessions: set[str] = set()
        seen_tmux_sessions: set[str] = set()
        while record.repair_attempt < MAX_REPAIR_CYCLES:
            next_attempt = record.repair_attempt + 1
            if self.retention is not None:
                try:
                    self.retention.before_attempt(record.candidate, next_attempt)
                except UpgradeManagerError as exc:
                    record.state = UpgradeState.CHECK_FAILED
                    record.last_failure_codes = [exc.code]
                    self.store.write_state(record)
                    return self.status()
            record.repair_attempt += 1
            record.state = UpgradeState.REPAIRING
            self.store.write_state(record)
            evidence_path = Path(record.evidence_path)
            try:
                evidence_sha256 = self.store.evidence_digest(record.repair_attempt, evidence_path)
                session = self.engineering.start(
                    record.candidate, evidence_path, record.repair_attempt
                )
            except UpgradeManagerError as exc:
                record.state = UpgradeState.CHECK_FAILED
                record.last_failure_codes = [exc.code]
                self.store.write_state(record)
                return self.status()
            try:
                evidence_unchanged = (
                    self.store.evidence_digest(record.repair_attempt, evidence_path)
                    == evidence_sha256
                )
            except UpgradeManagerError:
                evidence_unchanged = False
            session_valid = (
                session.fresh_session
                and session.codex_process_started
                and session.worktree_clean_at_start
                and session.changed_scope_validated
                and session.production_unchanged
                and session.evidence_sha256 == evidence_sha256
                and evidence_unchanged
                and session.session_id not in seen_sessions
                and session.tmux_session_name not in seen_tmux_sessions
                and Path(session.worktree_path).is_absolute()
            )
            if not session_valid:
                record.state = UpgradeState.CHECK_FAILED
                record.last_failure_codes = ["engineering_session_not_fresh"]
                try:
                    self.engineering.close(session.session_id)
                except UpgradeManagerError:
                    record.last_failure_codes.append("engineering_session_close_failed")
                self.store.write_state(record)
                return self.status()
            seen_sessions.add(session.session_id)
            seen_tmux_sessions.add(session.tmux_session_name)
            binding = record.candidate.binding.model_copy(
                update={
                    "cus_commit": session.cus_commit,
                    "telegram_commit": session.telegram_commit,
                    "combined_overlay_identity": session.combined_overlay_identity,
                }
            )
            record.candidate = record.candidate.model_copy(update={"binding": binding})
            self.store.write_candidate(record.candidate)
            try:
                raw_result = self.gate.run(record.candidate)
            except UpgradeManagerError as exc:
                record.state = UpgradeState.CHECK_FAILED
                record.last_failure_codes = [exc.code]
                record.last_gate = CompatibilityGateResult(
                    verdict=GateVerdict.CHECK_FAILED,
                    binding=record.candidate.binding,
                    checks=[],
                    failure_codes=[exc.code],
                )
            else:
                normalized = normalize_gate_result(record.candidate, raw_result)
                record.last_gate = normalized
                record.last_failure_codes = normalized.failure_codes
                if normalized.verdict is GateVerdict.COMPATIBLE:
                    record.state = UpgradeState.COMPATIBLE
                    record.compatibility_pass = CompatibilityPass(
                        binding=record.candidate.binding, checks=normalized.checks
                    )
                elif normalized.verdict is GateVerdict.CHECK_FAILED:
                    record.state = UpgradeState.CHECK_FAILED
                else:
                    record.state = UpgradeState.INCOMPATIBLE
            try:
                self.engineering.close(session.session_id)
            except UpgradeManagerError:
                record.state = UpgradeState.CHECK_FAILED
                record.last_failure_codes = ["engineering_session_close_failed"]
            self.store.write_state(record)
            if self.retention is not None:
                try:
                    self.retention.complete_attempt(
                        record.candidate,
                        record.repair_attempt,
                        session,
                        record.last_gate,
                        record.last_failure_codes,
                    )
                except UpgradeManagerError:
                    # The retention adapter persists PENDING/FAILED state for
                    # the scheduled idempotent retry. Compatibility truth is
                    # intentionally left untouched.
                    pass
            if record.state in {UpgradeState.COMPATIBLE, UpgradeState.CHECK_FAILED}:
                return self.status()
            if (
                record.state is UpgradeState.INCOMPATIBLE
                and record.repair_attempt < MAX_REPAIR_CYCLES
            ):
                path = self.store.write_evidence(
                    record.repair_attempt + 1,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "binding": record.candidate.binding.model_dump(mode="json"),
                        "failure_codes": normalized.failure_codes,
                        "checks": [check.model_dump(mode="json") for check in normalized.checks],
                    },
                )
                record.evidence_path = str(path)
                self.store.write_state(record)
        record.state = UpgradeState.HUMAN_REQUIRED
        self.store.write_state(record)
        return self.status()

    @_serialized_operation
    def update(
        self,
        *,
        operation_id: str | None = None,
        runner_identity: dict[str, str] | None = None,
    ) -> UpgradeStatus:
        record = self._record()
        if (
            record.state is not UpgradeState.COMPATIBLE
            or record.candidate is None
            or record.compatibility_pass is None
            or record.compatibility_pass.binding != record.candidate.binding
            or record.candidate.binding.compatibility_gate_version
            != self.compatibility_gate_version
        ):
            raise UpgradeManagerError("matching_compatibility_pass_required")
        candidate_at_click = record.candidate
        started_at = self.now().isoformat()
        record.state = UpgradeState.UPDATING
        self.store.write_state(record)
        applied: TransactionResult | None = None
        rollback: TransactionResult | None = None
        try:
            applied = self.activation.apply(record.candidate)
            failed = self._transaction_failures(
                record.candidate, applied, REQUIRED_ACTIVATION_STEPS
            )
        except UpgradeManagerError as exc:
            failed = [exc.code]
        if not failed:
            record.state = UpgradeState.ACTIVE
            record.active_version = record.candidate.binding.version
            record.last_failure_codes = []
            self.store.write_state(record)
            if self.retention is not None and applied is not None:
                try:
                    self.retention.after_active(record.candidate, applied)
                except UpgradeManagerError:
                    # A working Codex remains ACTIVE when cleanup is pending.
                    pass
            self._write_operator_action(
                operation_id=operation_id,
                action="UPDATE",
                candidate=candidate_at_click,
                record=record,
                runner_identity=runner_identity,
                started_at=started_at,
                transaction=applied,
            )
            return self.status()
        record.state = UpgradeState.ROLLING_BACK
        record.last_failure_codes = failed
        self.store.write_state(record)
        try:
            rollback = self.activation.rollback(record.candidate)
            rollback_failed = self._transaction_failures(
                record.candidate, rollback, REQUIRED_ROLLBACK_STEPS
            )
        except UpgradeManagerError as exc:
            rollback_failed = [exc.code]
        if rollback_failed:
            record.state = UpgradeState.BLOCKED
            record.last_failure_codes = failed + rollback_failed
        else:
            record.state = UpgradeState.ROLLBACK
        self.store.write_state(record)
        self._write_operator_action(
            operation_id=operation_id,
            action="UPDATE",
            candidate=candidate_at_click,
            record=record,
            runner_identity=runner_identity,
            started_at=started_at,
            transaction=applied,
            rollback=rollback,
        )
        return self.status()

    @staticmethod
    def _transaction_failures(
        candidate: CandidateDescriptor,
        result: TransactionResult,
        required_steps: tuple[str, ...],
    ) -> list[str]:
        if result.binding != candidate.binding:
            return ["transaction_binding_mismatch"]
        if set(result.steps) != set(required_steps):
            return ["transaction_step_set_invalid"]
        return [
            f"{step}:{result.steps[step].value.lower()}"
            for step in required_steps
            if result.steps[step] is not StepStatus.PASS
        ]
