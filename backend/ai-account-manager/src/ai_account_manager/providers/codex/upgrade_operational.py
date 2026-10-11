from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


CONFIG_ENV = "CUS_CODEX_UPGRADE_CONFIG"
DEFAULT_PRODUCTION_CONFIG = Path("/etc/cus/codex-upgrade.json")
MAX_JSON_BYTES = 256 * 1024
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,159}$")
EvidenceClass = Literal[
    "CONTRACT_TEST",
    "FILESYSTEM_SIMULATION",
    "PROCESS_INTEGRATION",
    "DISPOSABLE_E2E",
    "REAL_CANDIDATE_E2E",
    "BLOCKED",
    "NOT_RUN",
]
T = TypeVar("T", bound=BaseModel)


class OperationalError(RuntimeError):
    def __init__(self, code: str) -> None:
        normalized = code if SAFE_CODE.fullmatch(code) else "OPERATIONAL_RUNNER_FAILED"
        super().__init__(normalized)
        self.code = normalized


class RunnerFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    error_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,159}$")


class OperationalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["qualification", "production"]
    production_enabled: bool = False
    root: str
    cus_repository: str
    telegram_repository: str
    production_cus_repository: str
    production_telegram_repository: str
    production_cus_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    production_telegram_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    approved_codex_binary: str
    dedicated_test_profile: str
    manager_runtime_root: str
    command_ledger_path: str
    canonical_manifest_path: str
    telegram_python: str
    overlay_root: str
    deployment_root: str
    active_reference: str
    active_launcher_path: str
    runtime_specs_path: str
    project_state_path: str
    compatibility_state_path: str
    overlay_active_reference: str
    pth_path: str
    service_state_path: str
    sessions_state_path: str
    engineering_root: str
    engineering_worker_executable: str
    canary_worker_executable: str
    canary_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=200)
    canary_reasoning_effort: str = Field(default="high", min_length=1, max_length=80)
    qualification_fixture: bool = False
    fixture_candidate_binary: str | None = None
    gate_fixture_failures: int = Field(default=0, ge=0, le=6)
    gate_fixture_counter_path: str | None = None
    activation_fault_after: str | None = Field(default=None, max_length=80)
    rollback_fault_after: str | None = Field(default=None, max_length=80)
    engineering_fixture: bool = False

    @field_validator(
        "root",
        "cus_repository",
        "telegram_repository",
        "production_cus_repository",
        "production_telegram_repository",
        "approved_codex_binary",
        "dedicated_test_profile",
        "manager_runtime_root",
        "command_ledger_path",
        "canonical_manifest_path",
        "telegram_python",
        "overlay_root",
        "deployment_root",
        "active_reference",
        "active_launcher_path",
        "runtime_specs_path",
        "project_state_path",
        "compatibility_state_path",
        "overlay_active_reference",
        "pth_path",
        "service_state_path",
        "sessions_state_path",
        "engineering_root",
        "engineering_worker_executable",
        "canary_worker_executable",
    )
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("absolute non-root path required")
        return value

    @field_validator("fixture_candidate_binary", "gate_fixture_counter_path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise ValueError("absolute non-root path required")
        return value


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    returncode: int
    stdout: bytes
    stderr: bytes
    started_at: str
    ended_at: str
    duration_ms: int


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def assert_private_directory(path: Path, *, create: bool, code: str) -> Path:
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise OperationalError(code)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise OperationalError(code)
    if not path.exists():
        if not create:
            raise OperationalError(code)
        path.mkdir(mode=0o700, parents=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise OperationalError(code)
    return path.resolve(strict=True)


def canonical_path(
    path: Path,
    *,
    code: str,
    must_exist: bool = True,
    allowed_root: Path | None = None,
) -> Path:
    if not path.is_absolute() or ".." in path.parts or path == Path("/") or path.is_symlink():
        raise OperationalError(code)
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise OperationalError(code) from exc
    if str(path) != str(resolved):
        raise OperationalError(code)
    if allowed_root is not None:
        root = allowed_root.resolve(strict=True)
        if resolved != root and not is_beneath(resolved, root):
            raise OperationalError(code)
    return resolved


def safe_executable(path: Path, *, code: str, allowed_root: Path | None = None) -> Path:
    resolved = canonical_path(path, code=code, allowed_root=allowed_root)
    metadata = resolved.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
        or metadata.st_uid not in {0, os.geteuid()}
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise OperationalError(code)
    return resolved


def resolve_active_launcher(path: Path, *, code: str) -> Path:
    """Resolve the controlled Codex launcher symlink without accepting a mutable origin."""

    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise OperationalError(code)
    try:
        metadata = path.lstat()
        parent = path.parent.resolve(strict=True)
        parent_metadata = parent.lstat()
        raw_target = os.readlink(path)
    except OSError as exc:
        raise OperationalError(code) from exc
    if (
        not stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not Path(raw_target).is_absolute()
    ):
        raise OperationalError(code)
    return safe_executable(path.resolve(strict=True), code=code)


def read_private_bytes(path: Path, *, maximum: int = MAX_JSON_BYTES, code: str) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise OperationalError(code)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OperationalError(code) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > maximum
    ):
        raise OperationalError(code)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            payload = handle.read(maximum + 1)
    except OSError as exc:
        raise OperationalError(code) from exc
    if len(payload) > maximum:
        raise OperationalError(code)
    return payload


def read_private_model(path: Path, model: type[T], *, code: str) -> T:
    try:
        return model.model_validate_json(read_private_bytes(path, code=code))
    except (ValidationError, ValueError) as exc:
        raise OperationalError(code) from exc


def canonical_json(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise OperationalError("RESULT_PATH_UNSAFE")
    parent = assert_private_directory(path.parent, create=True, code="RESULT_PATH_UNSAFE")
    if path.is_symlink():
        raise OperationalError("RESULT_PATH_UNSAFE")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
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


def atomic_write_model(path: Path, value: BaseModel | dict[str, Any] | list[Any]) -> None:
    payload = canonical_json(value)
    if len(payload) > MAX_JSON_BYTES:
        raise OperationalError("RESULT_TOO_LARGE")
    atomic_write_bytes(path, payload)


def file_sha256(path: Path, *, code: str) -> str:
    resolved = canonical_path(path, code=code)
    before = resolved.lstat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = resolved.lstat()
    identity = lambda value: (  # noqa: E731
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise OperationalError("FILE_CHANGED_DURING_OBSERVATION")
    return digest.hexdigest()


def clean_environment(home: Path, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    assert_private_directory(home, create=True, code="COMMAND_HOME_UNSAFE")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }
    for key, value in (extra or {}).items():
        if key not in {"CODEX_HOME", "PYTHONPATH"} or "\0" in value:
            raise OperationalError("COMMAND_ENV_UNSAFE")
        environment[key] = value
    return environment


def run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    maximum_output: int = MAX_COMMAND_OUTPUT_BYTES,
) -> CommandResult:
    if (
        not argv
        or any(not isinstance(item, str) or not item or "\0" in item for item in argv)
        or timeout_seconds <= 0
        or timeout_seconds > 900
    ):
        raise OperationalError("COMMAND_INVALID")
    working = canonical_path(cwd, code="COMMAND_CWD_UNSAFE")
    started = time.monotonic()
    started_at = utc_now()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                argv,
                cwd=working,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=True,
                umask=0o077,
            )
        except OSError as exc:
            raise OperationalError("COMMAND_START_FAILED") from exc

        def terminate() -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()

        deadline = started + timeout_seconds
        while process.poll() is None:
            if time.monotonic() >= deadline:
                terminate()
                raise OperationalError("COMMAND_TIMEOUT")
            if (
                os.fstat(stdout_file.fileno()).st_size > maximum_output
                or os.fstat(stderr_file.fileno()).st_size > maximum_output
            ):
                terminate()
                raise OperationalError("COMMAND_OUTPUT_TOO_LARGE")
            time.sleep(0.02)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(maximum_output + 1)
        stderr = stderr_file.read(maximum_output + 1)
    if len(stdout) > maximum_output or len(stderr) > maximum_output:
        raise OperationalError("COMMAND_OUTPUT_TOO_LARGE")
    return CommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        started_at=started_at,
        ended_at=utc_now(),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def load_config(path: Path | None = None) -> OperationalConfig:
    selected = path or Path(os.environ.get(CONFIG_ENV, str(DEFAULT_PRODUCTION_CONFIG)))
    config = read_private_model(selected, OperationalConfig, code="RUNNER_CONFIG_INVALID")
    root = assert_private_directory(Path(config.root), create=False, code="RUNNER_ROOT_UNSAFE")
    if config.mode == "production":
        if not config.production_enabled:
            raise OperationalError("PRODUCTION_UPGRADE_DISABLED")
    else:
        mutable = (
            "manager_runtime_root",
            "command_ledger_path",
            "overlay_root",
            "deployment_root",
            "active_reference",
            "active_launcher_path",
            "runtime_specs_path",
            "project_state_path",
            "compatibility_state_path",
            "overlay_active_reference",
            "pth_path",
            "service_state_path",
            "sessions_state_path",
            "engineering_root",
        )
        for field in mutable:
            candidate = Path(getattr(config, field)).resolve(strict=False)
            if candidate != root and not is_beneath(candidate, root):
                raise OperationalError("QUALIFICATION_PATH_ESCAPE")
        if config.qualification_fixture:
            for raw in (config.fixture_candidate_binary, config.gate_fixture_counter_path):
                if raw is None:
                    raise OperationalError("QUALIFICATION_FIXTURE_INVALID")
                candidate = Path(raw).resolve(strict=False)
                if candidate != root and not is_beneath(candidate, root):
                    raise OperationalError("QUALIFICATION_PATH_ESCAPE")
        elif config.gate_fixture_failures:
            raise OperationalError("QUALIFICATION_FIXTURE_INVALID")
    return config


@contextmanager
def operation_lock(config: OperationalConfig, name: str) -> Iterator[None]:
    lock_root = assert_private_directory(
        Path(config.root) / "locks", create=True, code="OPERATION_LOCK_UNSAFE"
    )
    lock_path = lock_root / f"{name}.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationalError("OPERATION_IN_PROGRESS") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def append_ledger(
    config: OperationalConfig,
    *,
    runner: str,
    operation: str,
    argv: list[str] | None,
    cwd: Path,
    result: CommandResult | None,
    evidence_class: EvidenceClass,
    result_code: str,
) -> None:
    path = Path(config.command_ledger_path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    assert_private_directory(path.parent, create=True, code="LEDGER_PATH_UNSAFE")
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        records: list[dict[str, Any]] = []
        if path.exists():
            try:
                loaded = json.loads(read_private_bytes(path, code="LEDGER_INVALID"))
            except json.JSONDecodeError as exc:
                raise OperationalError("LEDGER_INVALID") from exc
            if not isinstance(loaded, list):
                raise OperationalError("LEDGER_INVALID")
            records = loaded
        now = utc_now()
        records.append(
            {
                "runner": runner,
                "operation": operation,
                "cwd": str(cwd),
                "argv": argv,
                "start": result.started_at if result else now,
                "end": result.ended_at if result else now,
                "duration_ms": result.duration_ms if result else 0,
                "exit_code": result.returncode if result else None,
                "stdout_sha256": (hashlib.sha256(result.stdout).hexdigest() if result else None),
                "stderr_sha256": (hashlib.sha256(result.stderr).hexdigest() if result else None),
                "stdout_bytes": len(result.stdout) if result else 0,
                "stderr_bytes": len(result.stderr) if result else 0,
                "evidence_class": evidence_class,
                "result_code": result_code,
            }
        )
        atomic_write_model(path, records)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


Handler = Callable[[str, Path, OperationalConfig], BaseModel]


def protocol_main(runner_name: str, handler: Handler, argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    operation, request_raw, result_raw = argv[1:]
    request_path = Path(request_raw)
    result_path = Path(result_raw)
    if not request_path.is_absolute() or not result_path.is_absolute():
        return 2
    try:
        config = load_config()
        request_payload = read_private_bytes(request_path, code="RUNNER_REQUEST_INVALID")
        key = hashlib.sha256(
            runner_name.encode()
            + b"\0"
            + operation.encode()
            + b"\0"
            + hashlib.sha256(canonical_json(config)).digest()
            + b"\0"
            + request_payload
        ).hexdigest()
        cache_root = assert_private_directory(
            Path(config.root) / "idempotency" / runner_name,
            create=True,
            code="IDEMPOTENCY_PATH_UNSAFE",
        )
        cache_path = cache_root / f"{key}.json"
        with operation_lock(config, runner_name):
            if cache_path.exists():
                atomic_write_bytes(
                    result_path,
                    read_private_bytes(cache_path, code="IDEMPOTENCY_RESULT_INVALID"),
                )
                return 0
            result = handler(operation, request_path, config)
            serialized = canonical_json(result)
            atomic_write_bytes(cache_path, serialized)
            atomic_write_bytes(result_path, serialized)
        return 0
    except OperationalError as exc:
        try:
            atomic_write_model(result_path, RunnerFailure(error_code=exc.code))
            return 0
        except OperationalError:
            return 2
